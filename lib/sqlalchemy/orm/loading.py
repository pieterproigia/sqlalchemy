# orm/loading.py
# Copyright (C) 2005-2014 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: http://www.opensource.org/licenses/mit-license.php

"""private module containing functions used to convert database
rows into object instances and associated state.

the functions here are called primarily by Query, Mapper,
as well as some of the attribute loading strategies.

"""
from __future__ import absolute_import

from .. import util
from . import attributes, exc as orm_exc
from ..sql import util as sql_util
from .util import _none_set, state_str
from .. import exc as sa_exc
import collections

_new_runid = util.counter()


def instances(query, cursor, context):
    """Return an ORM result as an iterator."""

    context.runid = _new_runid()

    filter_fns = [ent.filter_fn for ent in query._entities]
    filtered = id in filter_fns

    single_entity = len(query._entities) == 1 and \
        query._entities[0].supports_single_entity

    if filtered:
        if single_entity:
            filter_fn = id
        else:
            def filter_fn(row):
                return tuple(fn(x) for x, fn in zip(row, filter_fns))

    (process, labels) = \
        list(zip(*[
            query_entity.row_processor(query,
                                       context, cursor)
            for query_entity in query._entities
        ]))

    if not single_entity:
        keyed_tuple = util.lightweight_named_tuple('result', labels)

    while True:
        context.partials = {}

        if query._yield_per:
            fetch = cursor.fetchmany(query._yield_per)
            if not fetch:
                break
        else:
            fetch = cursor.fetchall()

        if single_entity:
            proc = process[0]
            rows = [proc(row) for row in fetch]
        else:
            rows = [keyed_tuple([proc(row) for proc in process])
                    for row in fetch]

        if filtered:
            rows = util.unique_list(rows, filter_fn)

        for row in rows:
            yield row

        if not query._yield_per:
            break


@util.dependencies("sqlalchemy.orm.query")
def merge_result(querylib, query, iterator, load=True):
    """Merge a result into this :class:`.Query` object's Session."""

    session = query.session
    if load:
        # flush current contents if we expect to load data
        session._autoflush()

    autoflush = session.autoflush
    try:
        session.autoflush = False
        single_entity = len(query._entities) == 1
        if single_entity:
            if isinstance(query._entities[0], querylib._MapperEntity):
                result = [session._merge(
                    attributes.instance_state(instance),
                    attributes.instance_dict(instance),
                    load=load, _recursive={})
                    for instance in iterator]
            else:
                result = list(iterator)
        else:
            mapped_entities = [i for i, e in enumerate(query._entities)
                               if isinstance(e, querylib._MapperEntity)]
            result = []
            keys = [ent._label_name for ent in query._entities]
            keyed_tuple = util.lightweight_named_tuple('result', keys)
            for row in iterator:
                newrow = list(row)
                for i in mapped_entities:
                    if newrow[i] is not None:
                        newrow[i] = session._merge(
                            attributes.instance_state(newrow[i]),
                            attributes.instance_dict(newrow[i]),
                            load=load, _recursive={})
                result.append(keyed_tuple(newrow))

        return iter(result)
    finally:
        session.autoflush = autoflush


def get_from_identity(session, key, passive):
    """Look up the given key in the given session's identity map,
    check the object for expired state if found.

    """
    instance = session.identity_map.get(key)
    if instance is not None:

        state = attributes.instance_state(instance)

        # expired - ensure it still exists
        if state.expired:
            if not passive & attributes.SQL_OK:
                # TODO: no coverage here
                return attributes.PASSIVE_NO_RESULT
            elif not passive & attributes.RELATED_OBJECT_OK:
                # this mode is used within a flush and the instance's
                # expired state will be checked soon enough, if necessary
                return instance
            try:
                state(state, passive)
            except orm_exc.ObjectDeletedError:
                session._remove_newly_deleted([state])
                return None
        return instance
    else:
        return None


def load_on_ident(query, key,
                  refresh_state=None, lockmode=None,
                  only_load_props=None):
    """Load the given identity key from the database."""

    if key is not None:
        ident = key[1]
    else:
        ident = None

    if refresh_state is None:
        q = query._clone()
        q._get_condition()
    else:
        q = query._clone()

    if ident is not None:
        mapper = query._mapper_zero()

        (_get_clause, _get_params) = mapper._get_clause

        # None present in ident - turn those comparisons
        # into "IS NULL"
        if None in ident:
            nones = set([
                        _get_params[col].key for col, value in
                        zip(mapper.primary_key, ident) if value is None
                        ])
            _get_clause = sql_util.adapt_criterion_to_null(
                _get_clause, nones)

        _get_clause = q._adapt_clause(_get_clause, True, False)
        q._criterion = _get_clause

        params = dict([
            (_get_params[primary_key].key, id_val)
            for id_val, primary_key in zip(ident, mapper.primary_key)
        ])

        q._params = params

    if lockmode is not None:
        version_check = True
        q = q.with_lockmode(lockmode)
    elif query._for_update_arg is not None:
        version_check = True
        q._for_update_arg = query._for_update_arg
    else:
        version_check = False

    q._get_options(
        populate_existing=bool(refresh_state),
        version_check=version_check,
        only_load_props=only_load_props,
        refresh_state=refresh_state)
    q._order_by = None

    try:
        return q.one()
    except orm_exc.NoResultFound:
        return None


def instance_processor(mapper, context, result, path, adapter,
                       polymorphic_from=None,
                       only_load_props=None,
                       refresh_state=None,
                       polymorphic_discriminator=None):
    """Produce a mapper level row processor callable
       which processes rows into mapped instances."""

    # note that this method, most of which exists in a closure
    # called _instance(), resists being broken out, as
    # attempts to do so tend to add significant function
    # call overhead.  _instance() is the most
    # performance-critical section in the whole ORM.

    pk_cols = mapper.primary_key

    if polymorphic_from or refresh_state:
        polymorphic_switch = None
    else:
        polymorphic_switch = _polymorphic_switch(
            context, mapper, result, path, polymorphic_discriminator, adapter)

    version_id_col = mapper.version_id_col

    if adapter:
        pk_cols = [adapter.columns[c] for c in pk_cols]
        if version_id_col is not None:
            version_id_col = adapter.columns[version_id_col]

    identity_class = mapper._identity_class

    populators = collections.defaultdict(list)

    props = mapper._props.values()
    if only_load_props is not None:
        props = (p for p in props if p.key in only_load_props)

    for prop in props:
        prop.create_row_processor(
            context, path, mapper, result, adapter, populators)

    eager_populators = populators.get('eager', ())

    load_path = context.query._current_path + path \
        if context.query._current_path.path else path

    session_identity_map = context.session.identity_map

    populate_existing = context.populate_existing or mapper.always_refresh
    load_evt = bool(mapper.class_manager.dispatch.load)
    refresh_evt = bool(mapper.class_manager.dispatch.refresh)
    instance_state = attributes.instance_state
    instance_dict = attributes.instance_dict
    session_id = context.session.hash_key
    version_check = context.version_check
    runid = context.runid

    if refresh_state:
        refresh_identity_key = refresh_state.key
        if refresh_identity_key is None:
            # super-rare condition; a refresh is being called
            # on a non-instance-key instance; this is meant to only
            # occur within a flush()
            refresh_identity_key = \
                mapper._identity_key_from_state(refresh_state)
    else:
        refresh_identity_key = None

    if mapper.allow_partial_pks:
        is_not_primary_key = _none_set.issuperset
    else:
        is_not_primary_key = _none_set.intersection

    def _instance(row):

        # if we are doing polymorphic, dispatch
        # to a different _instance() method specific to
        # the subclass mapper
        if polymorphic_switch is not None:
            result = polymorphic_switch(row)
            if result is not False:
                return result

        # determine the state that we'll be populating
        if refresh_identity_key:
            # fixed state that we're refreshing
            state = refresh_state
            instance = state.obj()
            dict_ = instance_dict(instance)
            isnew = state.runid != context.runid
            currentload = True
            loaded_instance = False
        else:
            # look at the row, see if that identity is in the
            # session, or we have to create a new one
            identitykey = (
                identity_class,
                tuple([row[column] for column in pk_cols])
            )

            instance = session_identity_map.get(identitykey)

            if instance is not None:
                # existing instance
                state = instance_state(instance)
                dict_ = instance_dict(instance)

                isnew = state.runid != runid
                currentload = not isnew
                loaded_instance = False

                if version_check and not currentload and \
                        version_id_col is not None and \
                        mapper._get_state_attr_by_column(
                            state,
                            dict_,
                            mapper.version_id_col) != \
                        row[version_id_col]:

                    raise orm_exc.StaleDataError(
                        "Instance '%s' has version id '%s' which "
                        "does not match database-loaded version id '%s'."
                        % (state_str(state),
                            mapper._get_state_attr_by_column(
                                state, dict_,
                                mapper.version_id_col),
                           row[version_id_col]))
            else:
                # create a new instance

                # check for non-NULL values in the primary key columns,
                # else no entity is returned for the row
                if is_not_primary_key(identitykey[1]):
                    return None

                isnew = True
                currentload = True
                loaded_instance = True

                instance = mapper.class_manager.new_instance()

                dict_ = instance_dict(instance)
                state = instance_state(instance)
                state.key = identitykey

                # attach instance to session.
                state.session_id = session_id
                session_identity_map._add_unpresent(state, identitykey)

        # populate.  this looks at whether this state is new
        # for this load or was existing, and whether or not this
        # row is the first row with this identity.
        if currentload or populate_existing:
            # full population routines.  Objects here are either
            # just created, or we are doing a populate_existing

            _populate_full(
                context, load_path, row, state, dict_, isnew,
                loaded_instance, populate_existing, populators)

            if isnew:
                if loaded_instance and load_evt:
                    state.manager.dispatch.load(state, context)
                elif isnew and refresh_evt:
                    state.manager.dispatch.refresh(
                        state, context, only_load_props)

                if populate_existing or state.modified:
                    if refresh_state and only_load_props:
                        state._commit(dict_, only_load_props)
                    else:
                        state._commit_all(dict_, session_identity_map)

        else:
            # partial population routines, for objects that were already
            # in the Session, but a row matches them; apply eager loaders
            # on existing objects, etc.
            unloaded = state.unloaded
            isnew = state not in context.partials

            if not isnew or unloaded or eager_populators:
                # state is having a partial set of its attributes
                # refreshed.  Populate those attributes,
                # and add to the "context.partials" collection.

                to_load = _populate_partial(
                    context, load_path, row, state, dict_, isnew,
                    unloaded, populators)

                for key, pop in eager_populators:
                    if key not in unloaded:
                        pop(state, dict_, row)

                if isnew:
                    if refresh_evt:
                        state.manager.dispatch.refresh(state, context, to_load)

                    state._commit(dict_, to_load)

        return instance
    return _instance

from sqlalchemy.cloader import _populate_full

def _dont_populate_full(
        context, load_path, row, state, dict_, isnew,
        loaded_instance, populate_existing, populators):
    if isnew:
        # first time we are seeing a row with this identity.
        state.runid = context.runid
        if context.propagate_options:
            state.load_options = context.propagate_options
        if state.load_options:
            state.load_path = load_path

        for key, getter in populators["quick"]:
            dict_[key] = getter(row)
        if populate_existing:
            for key, set_callable in populators["expire"]:
                dict_.pop(key, None)
                if set_callable:
                    state.callables[key] = state
        else:
            for key, set_callable in populators["expire"]:
                if set_callable:
                    state.callables[key] = state
        for key, populator in populators["new"]:
            populator(state, dict_, row)
        for key, populator in populators["delayed"]:
            populator(state, dict_, row)

    else:
        # have already seen rows with this identity.
        for key, populator in populators["existing"]:
            populator(state, dict_, row)


def _populate_partial(
        context, load_path, row, state, dict_, isnew,
        unloaded, populators):
    if not isnew:
        to_load = context.partials[state]
        for key, populator in populators["existing"]:
            if key not in to_load:
                continue
            populator(state, dict_, row)
    else:
        to_load = unloaded
        context.partials[state] = to_load

        if context.propagate_options:
            state.load_options = context.propagate_options
        if state.load_options:
            state.load_path = load_path

        for key, getter in populators["quick"]:
            if key not in to_load:
                continue
            dict_[key] = getter(row)
        for key, set_callable in populators["expire"]:
            if key not in to_load:
                continue
            dict_.pop(key, None)
            if set_callable:
                state.callables[key] = state
        for key, populator in populators["new"]:
            if key not in to_load:
                continue
            populator(state, dict_, row)
        for key, populator in populators["delayed"]:
            if key not in to_load:
                continue
            populator(state, dict_, row)

    return to_load


def _polymorphic_switch(
        context, mapper, result, path, polymorphic_discriminator, adapter):
    if polymorphic_discriminator is not None:
        polymorphic_on = polymorphic_discriminator
    else:
        polymorphic_on = mapper.polymorphic_on
    if polymorphic_on is None:
        return None

    def configure_subclass_mapper(discriminator):
        try:
            sub_mapper = mapper.polymorphic_map[discriminator]
        except KeyError:
            raise AssertionError(
                "No such polymorphic_identity %r is defined" %
                discriminator)
        if sub_mapper is mapper:
            return None

        return instance_processor(
            sub_mapper,
            context,
            result,
            path,
            adapter,
            polymorphic_from=mapper)

    polymorphic_instances = util.PopulateDict(
        configure_subclass_mapper
    )

    if adapter:
        polymorphic_on = adapter.columns[polymorphic_on]

    def polymorphic_instance(row):
        discriminator = row[polymorphic_on]
        if discriminator is not None:
            _instance = polymorphic_instances[discriminator]
            if _instance:
                return _instance(row)
            else:
                return False
    return polymorphic_instance


def load_scalar_attributes(mapper, state, attribute_names):
    """initiate a column-based attribute refresh operation."""

    # assert mapper is _state_mapper(state)
    session = state.session
    if not session:
        raise orm_exc.DetachedInstanceError(
            "Instance %s is not bound to a Session; "
            "attribute refresh operation cannot proceed" %
            (state_str(state)))

    has_key = bool(state.key)

    result = False

    if mapper.inherits and not mapper.concrete:
        statement = mapper._optimized_get_statement(state, attribute_names)
        if statement is not None:
            result = load_on_ident(
                session.query(mapper).from_statement(statement),
                None,
                only_load_props=attribute_names,
                refresh_state=state
            )

    if result is False:
        if has_key:
            identity_key = state.key
        else:
            # this codepath is rare - only valid when inside a flush, and the
            # object is becoming persistent but hasn't yet been assigned
            # an identity_key.
            # check here to ensure we have the attrs we need.
            pk_attrs = [mapper._columntoproperty[col].key
                        for col in mapper.primary_key]
            if state.expired_attributes.intersection(pk_attrs):
                raise sa_exc.InvalidRequestError(
                    "Instance %s cannot be refreshed - it's not "
                    " persistent and does not "
                    "contain a full primary key." % state_str(state))
            identity_key = mapper._identity_key_from_state(state)

        if (_none_set.issubset(identity_key) and
                not mapper.allow_partial_pks) or \
                _none_set.issuperset(identity_key):
            util.warn("Instance %s to be refreshed doesn't "
                      "contain a full primary key - can't be refreshed "
                      "(and shouldn't be expired, either)."
                      % state_str(state))
            return

        result = load_on_ident(
            session.query(mapper),
            identity_key,
            refresh_state=state,
            only_load_props=attribute_names)

    # if instance is pending, a refresh operation
    # may not complete (even if PK attributes are assigned)
    if has_key and result is None:
        raise orm_exc.ObjectDeletedError(state)

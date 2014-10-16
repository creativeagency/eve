# -*- coding: utf-8 -*-

"""
    eve.methods.patch
    ~~~~~~~~~~~~~~~~~

    This module imlements the PATCH method.

    :copyright: (c) 2014 by Nicola Iarocci.
    :license: BSD, see LICENSE for more details.
"""

from flask import current_app as app, abort
from werkzeug import exceptions
from datetime import datetime
from eve.utils import config, debug_error_message, parse_request
from eve.auth import requires_auth
from eve.validation import ValidationError
from eve.methods.common import get_document, parse, payload as payload_, \
    ratelimit, pre_event, store_media_files, resolve_embedded_fields, \
    build_response_document, marshal_write_response, resolve_document_etag
from eve.versioning import resolve_document_version, \
    insert_versioning_documents, late_versioning_catch


@ratelimit()
@requires_auth('item')
@pre_event
def patch(resource, payload=None, **lookup):
    """
    Default function for handling PATCH requests, it has decorators for
    rate limiting, authentication and for raising pre-request events.
    After the decorators are applied forwards to call to :func:`patch_internal`

    .. versionchanged:: 0.5
       Split into patch() and patch_internal().
    """
    return patch_internal(resource, payload, concurrency_check=True, **lookup)


def patch_internal(resource, payload=None, concurrency_check=False, **lookup):
    """ Intended for internal patch calls, this method is not rate limited,
    authentication is not checked, pre-request events are not raised, and
    concurrency checking is optional. Performs a document patch/update.
    Updates are first validated against the resource schema. If validation
    passes, the document is updated and an OK status update is returned.
    If validation fails, a set of validation issues is returned.

    :param resource: the name of the resource to which the document belongs.
    :param payload: alternative payload. When calling patch() from your own
                    code you can provide an alternative payload. This can be
                    useful, for example, when you have a callback function
                    hooked to a certain endpoint, and want to perform
                    additional patch() callsfrom there.

                    Please be advised that in order to successfully use this
                    option, a request context must be available.
    :param concurrency_check: concurrency check switch (bool)
    :param **lookup: document lookup query.

    .. versionchanged:: 0.5
       Original patch() has been split into patch() and patch_internal().
       You can now pass a pre-defined custom payload to the funcion.
       ETAG is now stored with the document (#369).
       Catching all HTTPExceptions and returning them to the caller, allowing
       for eventual flask.abort() invocations in callback functions to go
       through. Fixes #395.

    .. versionchanged:: 0.4
       Allow abort() to be inoked by callback functions.
       'on_update' raised before performing the update on the database.
       Support for document versioning.
       'on_updated' raised after performing the update on the database.

    .. versionchanged:: 0.3
       Support for media fields.
       When IF_MATCH is disabled, no etag is included in the payload.
       Support for new validation format introduced with Cerberus v0.5.

    .. versionchanged:: 0.2
       Use the new STATUS setting.
       Use the new ISSUES setting.
       Raise 'on_pre_<method>' event.

    .. versionchanged:: 0.1.1
       Item-identifier wrapper stripped from both request and response payload.

    .. versionchanged:: 0.1.0
       Support for optional HATEOAS.
       Re-raises `exceptions.Unauthorized`, this could occur if the
       `auth_field` condition fails

    .. versionchanged:: 0.0.9
       More informative error messages.
       Support for Python 3.3.

    .. versionchanged:: 0.0.8
       Let ``werkzeug.exceptions.InternalServerError`` go through as they have
       probably been explicitly raised by the data driver.

    .. versionchanged:: 0.0.7
       Support for Rate-Limiting.

    .. versionchanged:: 0.0.6
       ETag is now computed without the need of an additional db lookup

    .. versionchanged:: 0.0.5
       Support for 'aplication/json' Content-Type.

    .. versionchanged:: 0.0.4
       Added the ``requires_auth`` decorator.

    .. versionchanged:: 0.0.3
       JSON links. Superflous ``response`` container removed.
    """
    if payload is None:
        payload = payload_()

    original = get_document(resource, concurrency_check, **lookup)
    if not original:
        # not found
        abort(404)

    resource_def = app.config['DOMAIN'][resource]
    schema = resource_def['schema']
    validator = app.validator(schema, resource)

    object_id = original[config.ID_FIELD]
    last_modified = None
    etag = None

    issues = {}
    response = {}

    if config.BANDWIDTH_SAVER is True:
        embedded_fields = []
    else:
        req = parse_request(resource)
        embedded_fields = resolve_embedded_fields(resource, req)

    try:
        updates = parse(payload, resource)
        validation = validator.validate_update(updates, object_id, original)
        if validation:
            # sneak in a shadow copy if it wasn't already there
            late_versioning_catch(original, resource)

            store_media_files(updates, resource, original)
            resolve_document_version(updates, resource, 'PATCH', original)

            # some datetime precision magic
            updates[config.LAST_UPDATED] = \
                datetime.utcnow().replace(microsecond=0)

            # the mongo driver has a different precision than the python
            # datetime. since we don't want to reload the document once it has
            # been updated, and we still have to provide an updated etag,
            # we're going to update the local version of the 'original'
            # document, and we will use it for the etag computation.
            updated = original.copy()

            # notify callbacks
            getattr(app, "on_update")(resource, updates, original)
            getattr(app, "on_update_%s" % resource)(updates, original)

            updated.update(updates)

            if config.IF_MATCH:
                resolve_document_etag(updated)
                # now storing the (updated) ETAG with every document (#453)
                updates[config.ETAG] = updated[config.ETAG]

            app.data.update(resource, object_id, updates)
            insert_versioning_documents(resource, updated)

            # nofity callbacks
            getattr(app, "on_updated")(resource, updates, original)
            getattr(app, "on_updated_%s" % resource)(updates, original)

            # build the full response document
            build_response_document(
                updated, resource, embedded_fields, updated)
            response = updated

        else:
            issues = validator.errors
    except ValidationError as e:
        # TODO should probably log the error and abort 400 instead (when we
        # got logging)
        issues['validator exception'] = str(e)
    except exceptions.HTTPException as e:
        raise e
    except Exception as e:
        # consider all other exceptions as Bad Requests
        abort(400, description=debug_error_message(
            'An exception occurred: %s' % e
        ))

    if len(issues):
        response[config.ISSUES] = issues
        response[config.STATUS] = config.STATUS_ERR
        status = config.VALIDATION_ERROR_STATUS
    else:
        response[config.STATUS] = config.STATUS_OK
        status = 200

    # limit what actually gets sent to minimize bandwidth usage
    response = marshal_write_response(response, resource)

    return response, last_modified, etag, status

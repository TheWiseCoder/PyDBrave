import json
import os
import sys
from flask import (
    Blueprint, Flask, Response, jsonify, request, send_from_directory
)
from flask_cors import CORS
from flask_swagger_ui import get_swaggerui_blueprint
from pathlib import Path
from sqlalchemy.sql.elements import Type
from typing import Final

os.environ["PYPOMES_APP_PREFIX"] = "PYDB"
os.environ["PYDB_VALIDATION_MSG_PREFIX"] = ""

# ruff: noqa: E402
from pypomes_core import (
    get_versions, exc_format,
    str_lower, str_as_list, validate_format_errors
)  # noqa: PyPep8
from pypomes_http import (
    http_get_parameter, http_get_parameters
)  # noqa: PyPep8
from pypomes_logging import (
    PYPOMES_LOGGER,
    logging_send_entries, logging_log_info, logging_log_error
)  # noqa: PyPep8

from migration import (
    pydb_common, pydb_validator, pydb_migrator
)  # noqa: PyPep8

# establish the current version
APP_VERSION: Final[str] = "1.2.4"

# create the Flask application
app: Flask = Flask(__name__)

# support cross-origin resource sharing
CORS(app)

# configure jsonify() with 'ensure_ascii=False'
app.config["JSON_AS_ASCII"] = False

# make PyDBrief's API available as a Swagger app
swagger_blueprint: Blueprint = get_swaggerui_blueprint(
    base_url="/swagger",
    api_url="/swagger/pydbrief.json",
    config={"defaultModelsExpandDepth": -1}
)
app.register_blueprint(swagger_blueprint)


@app.route("/swagger/pydbrief.json")
def swagger() -> Response:
    """
    Entry point for the microservice providing OpenAPI specifications in the Swagger standard.

    By default, the browser is instructed to save the file, instead of displaying its contents.

    This parameter can optionally be provided to indicate otherwise:
        - attach=<1|t|true|0|f|false>: 'true' if not specified

    :return: the requested OpenAPI specifications
    """
    # define the treatment to be given to the file by the client
    param: str = http_get_parameter(request, "attach")
    attach: bool = isinstance(param, str) and param.lower() in ["1", "t", "true"]

    return send_from_directory(directory=Path(Path.cwd(), "swagger"),
                               path="pydbrief.json",
                               as_attachment=attach)


@app.route(rule="/version",
           methods=["GET"])
def version() -> Response:
    """
    Obtain the current version of *PyDBrief*, along with the *PyPomes* modules in use.

    :return: the versions in execution
    """
    # register the request
    logging_log_info(f"Request {request.path}")

    versions: dict = get_versions()
    versions["PyDBrief"] = APP_VERSION

    # assign to the return variable
    result: Response = jsonify(versions)

    # log the response
    logging_log_info(msg=f"Response {request.path}: {result}")

    return result


@app.route(rule="/get-log",
           methods=["GET"])
def get_log() -> Response:
    """
    Entry pointy for obtaining the execution log of the system.

    These criteria are specified as imput parameters of the HTTP request, according to the pattern
    *attach=<[t,true,f,false]>&log-path=<log-path>&level=<log-level>&
     from-datetime=YYYYMMDDhhmmss&to-datetime=YYYYMMDDhhmmss&last-days=<n>&last-hours=<n>>*

    The query parameters are optional, and are used to filter the records to be returned:
        - *attach*: whether browser should display or persist file (defaults to True - persist it)
        - *level*: <log-level>
        - *from-datetime*: <YYYYMMDDhhmmss>
        - *to-datetime*: <YYYYMMDDhhmmss>
        - *last-days*: <n>
        - *last-hours*: <n>

    By default, the browser is instructed to save the file, instead of displaying its contents.

    This parameter can optionally be provided to indicate otherwise:
        - *attach*=<1|t|true|0|f|false> - defaults to 'true'

    :return: the requested log data
    """
    # register the request
    req_query: str = request.query_string.decode()
    logging_log_info(f"Request {request.path}?{req_query}")

    # run the request
    result: Response = logging_send_entries(request=request)

    # log the response
    logging_log_info(f"Response {request.path}?{req_query}: {result}")

    return result


@app.route(rule="/rdbms",
           methods=["POST"])
@app.route(rule="/rdbms/<rdbms>",
           methods=["GET"])
def handle_rdbms(rdbms: str = None) -> Response:
    """
    Entry point for configuring the RDBMS to use.

    The parameters are as follows:
        - *db-engine*: the reference RDBMS engine (*mysql*, *oracle*, *postgres*, or *sqlserver*)
        - *db-name*: name of database
        - *db-user*: the logon user
        - *db-pwd*: the logon password
        - *db-host*: the host URL
        - *db-port*: the connection port
        - *db-client*: the client package (Oracle, only)
        - *db-driver*: the database access driver (SQLServer, only)

    :param rdbms: the reference RDBMS engine (*mysql*, *oracle*, *postgres*, or *sqlserver*)
    :return: the operation outcome
    """
    # initialize the errors list
    errors: list[str] = []

    # retrieve the input parameters
    scheme: dict = http_get_parameters(request=request)

    reply: dict | None = None
    if request.method == "GET":
        # get RDBMS connection params
        reply = pydb_common.get_connection_params(errors=errors,
                                                  rdbms=rdbms)
    else:
        # configure the RDBMS
        pydb_common.set_connection_params(errors=errors,
                                          scheme=scheme)
        if not errors:
            rdbms = scheme.get("db-engine")
            reply = {"status": f"RDBMS '{rdbms}' configuration updated"}

    # build the response
    result: Response = _build_response(errors=errors,
                                       reply=reply)
    # log the response
    logging_log_info(f"Response {request.path}?{scheme}: {result}")

    return result


@app.route(rule="/migration:configure",
           methods=["GET", "PATCH"])
@app.route(rule="/migration:verify",
           methods=["POST"])
def handle_migration() -> Response:
    """
    Entry point for configuring the RDBMS-independent parameters, and for assessing migration readiness.

    Assessing the server's migration readiness means to verify whether its state and data
    are valid and consistent, thus allowing for a migration to be attempted.

    For configuring, these are the expected parameters:
        - *batch-size*: maximum number of rows to migrate per batch (defaults to 1000000)
        - *chunk-size*: maximum size, in bytes, of data chunks in LOB data copying (defaults to 1048576)
        - *max-processes*: the number of processes to speed-up the migration with (defaults to 1)

    :return: the operation outcome
    """
    # initialize the errors list
    errors: list[str] = []

    # retrieve the input parameters
    scheme: dict = http_get_parameters(request=request)

    reply: dict | None = None
    match request.method:
        case "GET":
            # retrieve the migration parameters
            reply = pydb_common.get_migration_params()
        case "PATCH":
            # establish the migration parameters
            pydb_common.set_migration_params(errors=errors,
                                             scheme=scheme,
                                             logger=PYPOMES_LOGGER)
            if not errors:
                reply = {"status": "Configuration updated"}
        case "POST":
            # validate the source and target RDBMS engines
            pydb_validator.assert_rdbms_dual(errors=errors,
                                             scheme=scheme)
            # errors ?
            if not errors:
                # no, assert the migration parameters
                pydb_validator.assert_migration_params(errors=errors)
                # errors ?
                if errors:
                    # yes, report the problems
                    reply = {"status": "Migration cannot be launched"}
                else:
                    # no, display the migration context
                    reply = pydb_validator.get_migration_context(scheme=scheme)
                    reply.update({"status": "Migration can be launched"})

    # build the response
    result: Response = _build_response(errors=errors,
                                       reply=reply)
    # log the response
    logging_log_info(f"Response {request.path}?{scheme}: {result}")

    return result


@app.route(rule="/migrate",
           methods=["POST"])
def migrate_data() -> Response:
    """
    Migrate the specified schema/tables/views from the source to the target RDBMS.

    These are the expected parameters:
        - *from-rdbms*: the source RDBMS for the migration
        - *from-schema*: the source schema for the migration
        - *to-rdbms*: the destination RDBMS for the migration
        - *to-schema*: the destination schema for the migration
        - *migrate-metadata*: migrate metadata (this creates or transforms the destination schema)
        - *migrate-plaindata*: migrate non-LOB data
        - *migrate-lobdata*: migrate LOBs (large binary objects)
        - *process-indexes*: whether to migrate indexes (defaults to *False*)
        - *include-tables*: optional list of tables to migrate
        - *exclude-tables*: optional list of tables not to migrate
        - *include-views*: optional list of views to migrate ('*' migrates all views)
        - *exclude-columns*: optional list of table columns not to migrate
        - *skip-ck-constraints*: list of tables for which to skip check constraints
        - *skip-fk-constraints*: list of tables for which to skip foreign-key constraints
        - *skip-named-constraints*: list of names of constraints to skip

    These are noteworthy:
        - if *migrate-metadata* is not set, the following parameters are ignored: *process-indexes*,
          *include-views*, *skip-ck-constraints*, *skip-fk-constraints*, and *skip-named-constraints*;
        - the parameters *include-tables* and *exclude-tables* are mutually exclusive;
        - if *migrate-plaindata* is set, it is assumed that metadata is also being migrated,
          or all affected tables in destination schema exist and are empty;
        - if *migrate-lobdata* is set, it is assumed that plain data are also being,
          or have already been, migrated.

    :return: the operation outcome
    """
    # initialize the errors list
    errors: list[str] = []

    # retrieve the input parameters
    scheme: dict = http_get_parameters(request=request)

    # assert whether migration is warranted
    pydb_validator.assert_migration(errors=errors,
                                    scheme=scheme)
    reply: dict | None = None
    # is migration possible ?
    if not errors:
        # yes, establish the migration parameters
        source_rdbms: str = scheme.get("from-rdbms")
        target_rdbms: str = scheme.get("to-rdbms")
        source_schema: str = scheme.get("from-schema").lower()
        target_schema: str = scheme.get("to-schema").lower()
        step_metadata: bool = str_lower(scheme.get("migrate-metadata")) in ["1", "t", "true"]
        step_plaindata: bool = str_lower(scheme.get("migrate-plaindata")) in ["1", "t", "true"]
        step_lobdata: bool = str_lower(scheme.get("migrate-lobdata")) in ["1", "t", "true"]
        process_indexes: bool = step_metadata and str_lower(scheme.get("process-indexes")) in ["t", "true"]
        include_tables: list[str] = str_as_list(str_lower(scheme.get("include-tables"))) or []
        exclude_tables: list[str] = str_as_list(str_lower(scheme.get("exclude-tables"))) or []
        include_views: list[str] = str_as_list(str_lower(scheme.get("include-views"))) or []
        exclude_columns: list[str] = str_as_list(str_lower(scheme.get("exclude-columns"))) or []
        exclude_constraints: list[str] = str_as_list(str_lower(scheme.get("exclude-constraints"))) or []
        external_columns: dict[str, Type] = \
            pydb_validator.assert_column_types(errors=None,
                                               scheme=scheme)
        # migrate the data
        reply = pydb_migrator.migrate(errors=errors,
                                      source_rdbms=source_rdbms,
                                      target_rdbms=target_rdbms,
                                      source_schema=source_schema,
                                      target_schema=target_schema,
                                      step_metadata=step_metadata,
                                      step_plaindata=step_plaindata,
                                      step_lobdata=step_lobdata,
                                      process_indexes=process_indexes,
                                      include_tables=include_tables,
                                      exclude_tables=exclude_tables,
                                      include_views=include_views,
                                      exclude_columns=exclude_columns,
                                      exclude_constraints=exclude_constraints,
                                      external_columns=external_columns,
                                      version=APP_VERSION,
                                      logger=PYPOMES_LOGGER)
    # build the response
    result: Response = _build_response(errors=errors,
                                       reply=reply)
    # log the response
    logging_log_info(f"Response: {result}")

    return result


@app.errorhandler(code_or_exception=Exception)
def handle_exception(exc: Exception) -> Response:
    """
    Handle exceptions raised when responding to requests, but not handled.

    :return: status 500, with JSON containing the errors.
    """
    # import the needed exception
    from werkzeug.exceptions import NotFound

    # declare the return variable
    result: Response

    # is the exception an instance of werkzeug.exceptions.NotFound ?
    if isinstance(exc, NotFound):
        # yes, disregard it (with status 'No content')
        # (handles a bug causing the re-submission of a GET request from a browser)
        result = Response(status=204)
    else:
        # no, report the problem
        err_msg: str = exc_format(exc=exc,
                                  exc_info=sys.exc_info())
        logging_log_error(msg=f"{err_msg}")
        reply: dict = {
            "errors": [err_msg]
        }
        json_str: str = json.dumps(obj=reply,
                                   ensure_ascii=False)
        result = Response(response=json_str,
                          status=500,
                          mimetype="application/json")

    return result


def _build_response(errors: list[str],
                    reply: dict) -> Response:

    # declare the return variable
    result: Response

    if len(errors) == 0:
        # 'reply' might be None
        result = jsonify(reply)
    else:
        reply_err: dict = {"errors": validate_format_errors(errors=errors)}
        if isinstance(reply, dict):
            reply_err.update(reply)
        result = jsonify(reply_err)
        result.status_code = 400

    return result


if __name__ == "__main__":

    app.run(host="0.0.0.0",
            port=5000,
            debug=True)

from logging import Logger
from oracledb import Connection, connect, makedsn, init_oracle_client
from pypomes_core import validate_format_error

from .pydb_common import db_except_msg, db_log

# noinspection DuplicatedCode
ORCL_DB_CLIENT: str | None = None
ORCL_DB_NAME: str | None = None
ORCL_DB_USER: str | None = None
ORCL_DB_PWD: str | None = None
ORCL_DB_HOST: str | None = None
ORCL_DB_PORT: int | None = None

client_initialized: bool = False


def initialize(errors: list[str]) -> None:

    global client_initialized
    if isinstance(ORCL_DB_CLIENT, str) and not client_initialized:
        try:
            init_oracle_client(ORCL_DB_CLIENT)
            client_initialized = True
        except Exception as e:
            errors.append(db_except_msg(e, ORCL_DB_NAME, ORCL_DB_HOST))


def db_connect(errors: list[str],
               logger: Logger = None) -> Connection:
    """
    Obtain and return a connection to the database, or *None* if the connection could not be obtained.

    :param errors: incidental error messages
    :param logger: optional logger
    :return: the connection to the database
    """
    # initialize the return variable
    result: Connection | None = None

    # obtain a connection to the database
    err_msg: str | None = None
    try:
        result = connect(
            host=ORCL_DB_HOST,
            port=ORCL_DB_PORT,
            user=ORCL_DB_USER,
            password=ORCL_DB_PWD
        )
    except Exception as e:
        err_msg = db_except_msg(e, ORCL_DB_NAME, ORCL_DB_HOST)

    # log the results
    db_log(errors, err_msg, logger, f"Connecting to '{ORCL_DB_NAME}' at '{ORCL_DB_HOST}'")

    return result


def get_connection_params() -> dict:

    return {
        "rdbms": "oracle",
        "name": ORCL_DB_NAME,
        "user": ORCL_DB_USER,
        "password": ORCL_DB_PWD,
        "host": ORCL_DB_HOST,
        "port": ORCL_DB_PORT,
        "client": ORCL_DB_CLIENT
    }


def set_connection_params(errors: list[str],
                          scheme: dict) -> None:
    """
    Establish the parameters for connection to the Oracle engine.

    These are the parameters:
        - *db-name*: name of the database
        - *db-user*: name of logon user
        - *db-pwd*: password for login
        - *db-host*: host URL
        - *db-port*: host port
        - *db-client*: Oracle client

    :param errors: incidental error messages
    :param scheme: the provided parameters
    """
    # SQLServer-only parameter
    if scheme.get("db-driver"):
        # 113: Attribute not applicable for {}
        errors.append(validate_format_error(113, "Oracle", "@db-driver"))

    if scheme.get("db-port"):
        if scheme.get("db-port").isnumeric():
            global ORCL_DB_PORT
            ORCL_DB_PORT = int(scheme.get("db-port"))
        else:
            # 128: Invalid value {}: must be type {}
            errors.append(validate_format_error(128, "int", "@ORCL_DB_PORT"))

    # noinspection DuplicatedCode
    if len(errors) == 0:
        if scheme.get("db-name"):
            global ORCL_DB_NAME
            ORCL_DB_NAME = scheme.get("db-name")
        if scheme.get("db-user"):
            global ORCL_DB_USER
            ORCL_DB_USER = scheme.get("db-user")
        if scheme.get("db-pwd"):
            global ORCL_DB_PWD
            ORCL_DB_PWD = scheme.get("db-pwd")
        if scheme.get("db-client"):
            global ORCL_DB_CLIENT
            ORCL_DB_CLIENT = scheme.get("db-client")
        if scheme.get("db-host"):
            global ORCL_DB_HOST
            ORCL_DB_HOST = scheme.get("db-host")


def assert_connection_params(errors: list[str]) -> bool:
    """
    Assert that the parameters for connecting with the PostgreSQL engine have been provided.

    The *errors* argument will contain the appropriate messages regarding missing parameters.

    :param errors: incidental error messages
    :return: 'True' if all parameters have been provided, 'False' otherwise
    """
    # 112: Required attribute
    if not ORCL_DB_NAME:
        errors.append(validate_format_error(112, "@ORCL_DB_NAME"))
    if not ORCL_DB_USER:
        errors.append(validate_format_error(112, "@ORCL_DB_USER"))
    if not ORCL_DB_PWD:
        errors.append(validate_format_error(112, "@ORCL_DB_PWD"))
    if not ORCL_DB_HOST:
        errors.append(validate_format_error(112, "@ORCL_DB_HOST"))
    if not ORCL_DB_PORT:
        errors.append(validate_format_error(112, "@ORCL_DB_PORT"))

    return len(errors) == 0


def build_connection_string() -> str:

    dsn: str = makedsn(host=ORCL_DB_HOST,
                       port=ORCL_DB_PORT,
                       service_name=ORCL_DB_NAME)
    return f"oracle+oracledb://{ORCL_DB_USER}:{ORCL_DB_PWD}@{dsn}"


def build_select_query(schema: str,
                       table: str,
                       columns: str,
                       offset: int,
                       batch_size: int) -> str:
    return (
        f"SELECT {columns} "
        f"FROM {schema}.{table} "
        f"ORDER BY rowid "
        f"OFFSET {offset} ROWS FETCH NEXT {batch_size} ROWS ONLY"
    )


def get_table_unlog_stmt(schema: str,
                         table: str) -> str:

    return f"ALTER TABLE {schema}.{table} NOLOGGING;"

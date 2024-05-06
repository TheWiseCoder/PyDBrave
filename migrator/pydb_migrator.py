import sys
from logging import DEBUG, INFO, Logger
from pypomes_core import validate_format_error, exc_format
from pypomes_db import db_connect, db_select_all, db_bulk_insert
from sqlalchemy import text  # from 'sqlalchemy._elements._constructors', but invisible
from sqlalchemy.engine.base import Engine, RootTransaction
from sqlalchemy.engine.create import create_engine
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.engine.result import Result
from sqlalchemy.inspection import inspect
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.sql.schema import Column, MetaData, Table
from typing import Any

from . import (
    pydb_common, pydb_types, pydb_validator,
    pydb_oracle, pydb_postgres, pydb_sqlserver  # , pydb_mysql
)


# this is the entry point for the migration process
def migrate(errors: list[str],
            source_rdbms: str,
            target_rdbms: str,
            source_schema: str,
            target_schema: str,
            data_tables: list[str],
            logger: Logger | None) -> dict:

    pydb_common.log(logger, INFO,
                    "Started migrating the metadata")
    migrated_tables: list[dict] = migrate_metadata(errors, source_rdbms, target_rdbms,
                                                   source_schema, target_schema, data_tables, logger)
    pydb_common.log(logger, INFO,
                    "Finished migrating the metadata")

    pydb_common.log(logger, INFO,
                    "Started migrating the plain data")
    op_errors: list[str] = []
    # disable target RDBMS restrictions to speed-up bulk copying
    target_conn: Any = db_connect(errors=op_errors,
                                  engine=target_rdbms,
                                  logger=logger)
    if target_conn:
        disable_session_restrictions(op_errors, target_rdbms, target_conn, logger)
        if not op_errors:
            migrate_plain_data(op_errors, source_rdbms, target_rdbms,
                               source_schema, target_schema, target_conn, migrated_tables, logger)
        # restore target RDBMS restrictions delaying bulk copying
        restore_session_restrictions(op_errors, target_rdbms, target_conn, logger)
        target_conn.close()
    errors.extend(op_errors)
    pydb_common.log(logger, INFO,
                    "Finished migrating the plain data")

    return {
        "source": {
            "rdbms": source_rdbms,
            "schema": source_schema
        },
        "target": {
            "rdbms": target_rdbms,
            "schema": target_schema
        },
        "migrated-tables": migrated_tables
    }


# structure of migration data returned:
# [
#   {
#      "table": <table-name>,
#      "columns": [
#        {
#          "name": <column_name>,
#          "source-type": <column-type>,
#          "target-type": <column-type>
#        },
#        ...
#      ],
#      "count": <number-of-tuples-migrated>,
#      "status": "none" | "full" | "partial"
#   }
# ]
def migrate_metadata(errors: list[str],
                     source_rdbms: str,
                     target_rdbms: str,
                     source_schema: str,
                     target_schema: str,
                     data_tables: list[str],
                     logger: Logger | None) -> list[dict]:

    # iinitialize the return variable
    result: list[dict] = []

    # create engines
    source_engine: Engine = build_engine(errors, source_rdbms, logger)
    target_engine: Engine = build_engine(errors, target_rdbms, logger)

    # were both engines created ?
    if source_engine and target_engine:
        # yes, proceed
        from_schema: str | None = None

        # obtain the source schema's internal name
        inspector: Inspector = inspect(subject=source_engine,
                                       raiseerr=True)
        for schema_name in inspector.get_schema_names():
            # is this the source schema ?
            if source_schema.lower() == schema_name.lower():
                # yes, use the actual name with its case imprint
                from_schema = schema_name
                break

        # does the source schema exist ?
        if from_schema:
            # yes, proceed
            source_metadata: MetaData = MetaData(schema=from_schema)
            source_metadata.reflect(bind=source_engine,
                                    schema=from_schema)

            # build list of migration candidates
            unlisted_tables: list[str] = []
            if data_tables:
                table_names: list[str] = [table.name for table in source_metadata.sorted_tables]
                for spare_table in data_tables:
                    if spare_table not in table_names:
                        unlisted_tables.append(spare_table)

            # proceed, if all tables were found
            if len(unlisted_tables) == 0:
                # purge the source metadata from spare tables, if applicable
                if data_tables:
                    # build the list of spare tables in source metadata
                    spare_tables: list[Table] = []
                    for spare_table in source_metadata.sorted_tables:
                        if spare_table.name not in data_tables:
                            spare_tables.append(spare_table)
                    # remove the spare tables from the source metadata
                    for spare_table in spare_tables:
                        source_metadata.remove(table=spare_table)
                    # make sure spare tables will not hang around
                    del spare_tables

                # proceed with the appropriate tables
                source_tables: list[Table] = source_metadata.sorted_tables
                to_schema: str | None = None
                inspector = inspect(subject=target_engine,
                                    raiseerr=True)
                # obtain the target schema's internal name
                for schema_name in inspector.get_schema_names():
                    # is this the target schema ?
                    if target_schema.lower() == schema_name.lower():
                        # yes, use the actual name with its case imprint
                        to_schema = schema_name
                        break

                # does the target schema already exist ?
                if to_schema:
                    # yes, drop existing tables (must be done in reverse order)
                    for source_table in reversed(source_tables):
                        # build DROP TABLE statement
                        drop_stmt: str = f"DROP TABLE IF EXISTS {to_schema}.{source_table.name}"
                        # drop the table
                        engine_exc_stmt(errors, target_rdbms,
                                        target_engine, drop_stmt, logger)
                else:
                    # no, create the target schema
                    conn_params: dict = pydb_validator.get_connection_params(errors, target_rdbms)
                    stmt: str = f"CREATE SCHEMA {target_schema} AUTHORIZATION {conn_params.get('user')}"
                    engine_exc_stmt(errors, target_rdbms, target_engine, stmt, logger)

                    # SANITY CHECK: it has happened that a schema creation failed, with no errors reported
                    if len(errors) == 0:
                        inspector = inspect(subject=target_engine,
                                            raiseerr=True)
                        for schema_name in inspector.get_schema_names():
                            # is this the target schema ?
                            if target_schema.lower() == schema_name.lower():
                                # yes, use the actual name with its case imprint
                                to_schema = schema_name
                                break

                # does the target schema exist now ?
                if to_schema:
                    # yes, establish the migration equivalences
                    (native_ordinal, reference_ordinal) = \
                        pydb_types.establish_equivalences(source_rdbms, target_rdbms)

                    # setup target tables
                    for source_table in source_tables:

                        # build the list of migrated columns for this table
                        table_columns: list[dict] = []
                        # noinspection PyProtectedMember
                        for column in source_table.c._all_columns:
                            column_data: dict = {
                                "name": column.name,
                                "source-type": str(column.type)
                            }
                            table_columns.append(column_data)

                        # migrate the columns
                        columns: list[Column] = setup_target_table(errors, source_rdbms,
                                                                   target_rdbms, native_ordinal,
                                                                   reference_ordinal, source_table, logger)
                        source_table.schema = to_schema

                        # register the mew column types
                        for column in columns:
                            for table_column in table_columns:
                                if table_column["name"] == column.name:
                                    table_column["target-type"] = str(column.type)
                                    break

                        # register the migrated table
                        migrated_table: dict = {
                            "table": source_table.name,
                            "columns": table_columns,
                            "count": 0,
                            "status": "none"
                        }
                        result.append(migrated_table)

                    # create tables in target schema
                    try:
                        source_metadata.create_all(bind=target_engine,
                                                   checkfirst=False)
                    except Exception as e:
                        err_msg = exc_format(exc=e,
                                             exc_info=sys.exc_info())
                        # 104: Unexpected error: {}
                        errors.append(validate_format_error(104, err_msg))
                else:
                    # 104: Unexpected error: {}
                    errors.append(validate_format_error(104,
                                                        f"unable to create schema in RDBMS {target_rdbms}",
                                                        "@to-schema"))
            else:
                # tables not found, report them
                bad_tables: str = ", ".join(unlisted_tables)
                errors.append(validate_format_error(119, bad_tables,
                                                    f"not found in {source_rdbms}/{source_schema}",
                                                    "@tables"))
        else:
            # 119: Invalid value {}: {}
            errors.append(validate_format_error(119, source_schema,
                                                f"schema not found in RDBMS {source_rdbms}",
                                                "@from-schema"))
    return result


def migrate_plain_data(errors: list[str],
                       source_rdbms: str,
                       target_rdbms: str,
                       source_schema: str,
                       target_schema: str,
                       target_conn: Any,
                       migrated_tables: list[dict],
                       logger: Logger | None) -> None:

    # traverse list of migrated tables to copy the plain data
    for migrated_table in migrated_tables:
        offset: int = 0
        migrated_count: int = 0
        table: str = migrated_table.get("table")

        # disable table restrictions to speed-up bulk copying
        # disable_table_restrictions(errors, target_rdbms, target_schema, table, target_conn, logger)

        # exclude BLOB, CLOB, RAW, and other large binary types from the column names list
        table_columns: list[dict] = migrated_table.get("columns")
        column_names: list[str] = [column.get("name") for column in table_columns
                                   if not pydb_types.is_large_binary(column.get("source_type"))]

        # build the INSERT and SELECT statements
        insert_stmt = build_bulk_insert_stmt(target_rdbms, target_schema,
                                             table, column_names, logger)
        sel_stmt: str = build_bulk_select_stmt(source_rdbms, source_schema,
                                               table, column_names, offset,
                                               pydb_common.MIGRATION_BATCH_SIZE, logger)
        # copy the table data
        op_errors: list[str] = []
        bulk_data: list[tuple] = db_select_all(errors=errors,
                                               sel_stmt=sel_stmt,
                                               engine=source_rdbms,
                                               logger=logger)
        while not op_errors and bulk_data:
            # insert the current chunk of data
            db_bulk_insert(errors=op_errors,
                           insert_stmt=insert_stmt,
                           insert_vals=bulk_data,
                           engine=target_rdbms,
                           conn=target_conn,
                           logger=logger)
            # errors ?
            if len(op_errors) > 0:
                # yes, skip to next table
                break
            migrated_count += len(bulk_data)

            # fetch the next chunk of data
            offset += len(bulk_data)
            sel_stmt: str = build_bulk_select_stmt(source_rdbms, source_schema,
                                                   table, column_names, offset,
                                                   pydb_common.MIGRATION_BATCH_SIZE, logger)
            bulk_data: list[tuple] = db_select_all(errors=op_errors,
                                                   sel_stmt=sel_stmt,
                                                   engine=source_rdbms,
                                                   logger=logger)

        if op_errors:
            errors.extend(op_errors)
            status: str = "partial" if migrated_count else "none"
        else:
            status: str = "full"
        migrated_table["status"] = status
        migrated_table["count"] = migrated_count

        # restore table restrictions delaying bulk copying
        # restore_table_restrictions(errors, target_rdbms, target_schema, table, target_conn, logger)


def build_engine(errors: list[str],
                 rdbms: str,
                 logger: Logger) -> Engine:

    # initialize the return variable
    result: Engine | None = None

    # obtain the connection string
    conn_str: str = pydb_validator.get_connection_string(rdbms)

    # build the engine
    try:
        result = create_engine(url=conn_str)
        pydb_common.log(logger, DEBUG,
                        f"RDBMS {rdbms}, created engine")
    except Exception as e:
        params: dict = pydb_validator.get_connection_params(errors, rdbms)
        errors.append(pydb_common.db_except_msg(e, params.get("name"), params.get("host")))

    return result


def setup_target_table(errors: list[str],
                       source_rdbms: str,
                       target_rdbms: str,
                       native_ordinal: int,
                       reference_ordinal: int,
                       source_table: Table,
                       logger: Logger) -> list[Column]:

    # initialize the return variable
    result: list[Column] = []

    # set the target columns
    # noinspection PyProtectedMember
    for column in source_table.c._all_columns:
        # convert the type
        target_type: Any = pydb_types.migrate_type(source_rdbms, target_rdbms,
                                                   native_ordinal, reference_ordinal, column, logger)
        result.append(column)

        # wrap-up the column migration
        try:
            # set column's new type
            column.type = target_type

            # remove the server default value
            if hasattr(column, "server_default"):
                column.server_default = None

            # convert the default value - TODO: write a decent default value conversion function
            if hasattr(column, "default") and \
               column.default is not None and \
               column.lower() in ["sysdate", "systime"]:
                column.default = None
        except Exception as e:
            err_msg = exc_format(exc=e,
                                 exc_info=sys.exc_info())
            # 104: Unexpected error: {}
            errors.append(validate_format_error(104, err_msg))

    return result


def build_bulk_select_stmt(rdbms: str,
                           schema: str,
                           table: str,
                           columns: list[str],
                           offset: int,
                           batch_size: int,
                           logger: Logger) -> str:

    # initialize the return variable
    result: str | None = None

    # obtain names of columns
    columns_str: str = ", ".join(columns)

    # build the SELECT query
    match rdbms:
        case "mysql":
            pass
        case "oracle":
            result = pydb_oracle.build_bulk_select_stmt(schema, table,
                                                        columns_str, offset, batch_size)
        case "postgres":
            result = pydb_postgres.build_bulk_select_stmt(schema, table,
                                                          columns_str, offset, batch_size)
        case "sqlserver":
            result = pydb_sqlserver.build_bulk_select_stmt(schema, table,
                                                           columns_str, offset, batch_size)
    pydb_common.log(logger, DEBUG,
                    f"RDBMS {rdbms}, built query {result}")

    return result


def build_bulk_insert_stmt(rdbms: str,
                           schema: str,
                           table: str,
                           columns: list[str],
                           logger: Logger) -> str:

    # initialize the return variable
    result: str | None = None

    # obtain names of columns
    columns_str: str = ", ".join(columns)

    # build the SELECT query
    match rdbms:
        case "mysql":
            pass
        case "oracle":
            result = pydb_oracle.build_bulk_insert_stmt(schema, table, columns_str)
        case "postgres":
            result = pydb_postgres.build_bulk_insert_stmt(schema, table, columns_str)
        case "sqlserver":
            result = pydb_sqlserver.build_bulk_insert_stmt(schema, table, columns_str)
    pydb_common.log(logger, DEBUG,
                    f"RDBMS {rdbms}, built query {result}")

    return result


def disable_session_restrictions(errors: list[str],
                                 rdbms: str,
                                 conn: Any,
                                 logger: Logger) -> None:

    # disable session restrictions to speed-up bulk copy
    match rdbms:
        case "mysql":
            pass
        case "oracle":
            pydb_oracle.disable_session_restrictions(errors, conn, logger)
        case "postgres":
            pydb_postgres.disable_session_restrictions(errors, conn, logger)
        case "sqlserver":
            pydb_sqlserver.disable_session_restrictions(errors, conn, logger)

    pydb_common.log(logger, DEBUG,
                    f"RDBMS {rdbms}, disabled session restrictions to speed-up bulk copying")


def restore_session_restrictions(errors: list[str],
                                 rdbms: str,
                                 conn: Any,
                                 logger: Logger) -> None:

    # restore session restrictions delaying bulk copy
    match rdbms:
        case "mysql":
            pass
        case "oracle":
            pydb_oracle.restore_session_restrictions(errors, conn, logger)
        case "postgres":
            pydb_postgres.restore_session_restrictions(errors, conn, logger)
        case "sqlserver":
            pydb_sqlserver.restore_session_restrictions(errors, conn, logger)

    pydb_common.log(logger, DEBUG,
                    f"RDBMS {rdbms}, restored session restrictions delaying bulk copying")


def disable_table_restrictions(errors: list[str],
                               rdbms: str,
                               schema: str,
                               table: str,
                               conn: Any,
                               logger: Logger) -> None:

    # disable table restrictions delaying bulk copy
    match rdbms:
        case "mysql":
            pass
        case "oracle":
            pydb_oracle.disable_table_restrictions(errors, schema, table, conn, logger)
        case "postgres":
            pydb_postgres.disable_table_restrictions(errors, schema, table, conn, logger)
        case "sqlserver":
            pydb_sqlserver.disable_table_restrictions(errors, schema, table, conn, logger)

    pydb_common.log(logger, DEBUG,
                    f"Table {rdbms}.{schema}.{table}, disabled restrictions to speed-up bulk copying")


def restore_table_restrictions(errors: list[str],
                               rdbms: str,
                               schema: str,
                               table: str,
                               conn: Any,
                               logger: Logger) -> None:

    # restore table restrictions delaying bulk copy
    match rdbms:
        case "mysql":
            pass
        case "oracle":
            pydb_oracle.restore_table_restrictions(errors, schema, table, conn, logger)
        case "postgres":
            pydb_postgres.restore_table_restrictions(errors, schema, table, conn, logger)
        case "sqlserver":
            pydb_sqlserver.restore_table_restrictions(errors, schema, table, conn, logger)

    pydb_common.log(logger, DEBUG,
                    f"Table {rdbms}.{schema}.{table}, restored restrictions delaying bulk copying")


def engine_exc_stmt(errors: list[str],
                    rdbms: str,
                    engine: Engine,
                    stmt: str,
                    logger: Logger) -> Result:

    result: Result | None = None
    exc_stmt: TextClause = text(stmt)
    try:
        with engine.connect() as conn:
            trans: RootTransaction = conn.begin()
            result = conn.execute(statement=exc_stmt)
            trans.commit()
            pydb_common.log(logger, DEBUG,
                            f"RDBMS {rdbms}, sucessfully executed {stmt}")
    except Exception as e:
        err_msg = exc_format(exc=e,
                             exc_info=sys.exc_info())
        # 104: Unexpected error: {}
        errors.append(validate_format_error(104, err_msg))

    return result

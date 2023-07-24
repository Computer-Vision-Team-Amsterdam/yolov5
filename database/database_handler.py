import os
import subprocess
import json

from sqlalchemy import create_engine, Column, String, Boolean, Date, Integer, Float
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import SQLAlchemyError

from utils.general import LOGGER

LOCAL_RUN = False


def get_db_access_token(client_id):
    # Authenticate using Managed Identity (MSI)
    try:
        command = ["az", "login", "--identity", "--username", client_id]
        subprocess.check_call(command)
    except subprocess.CalledProcessError as e:
        print("Error during 'az login --identity':", e)
        raise e

    # Execute Azure CLI command to get the access token
    command = ["az", "account", "get-access-token", "--resource-type", "oss-rdbms"]
    output = subprocess.check_output(command)

    # Parse the output to retrieve the access token
    access_token = json.loads(output)["accessToken"]

    return access_token


def make_connection_string():
    # Load the JSON file
    with open('database.json') as f:
        config = json.load(f)

    # Retrieve values from the JSON
    hostname = config["hostname"]
    username = config["username"]
    database_name = config["database_name"]
    client_id = config["client_id"]
    password = get_db_access_token(client_id)

    db_url = f"postgresql://{username}:{password}@{hostname}/{database_name}"

    return db_url


def create_connection():
    try:
        # Create the engine
        if LOCAL_RUN:
            db_url = f"postgresql://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}@{os.environ['POSTGRES_HOST']}/{os.environ['POSTGRES_DB']}"
        else:
            db_url = make_connection_string()
        engine = create_engine(db_url)

        # Create and open a session
        engine = create_engine(db_url)
        Session = sessionmaker(bind=engine)

        with Session() as session:
            return engine, session

    except SQLAlchemyError as e:
        # Handle any exceptions that occur during connection creation
        LOGGER.error(f"Error creating database connection: {str(e)}")
        raise e


def close_connection(engine, session):
    try:
        # Close the session
        session.close()

    except SQLAlchemyError as e:
        LOGGER.error(f"Error closing database session: {str(e)}")
        raise e

    finally:
        try:
            # Dispose the engine
            engine.dispose()
        except SQLAlchemyError as e:
            LOGGER.error(f"Error disposing the database engine: {str(e)}")
            raise e

import subprocess
import json
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime, timedelta

from yolov5.utils.general import LOGGER

class DBConfigSQLAlchemy:
    Base = declarative_base()

    def __init__(self, db_username, db_hostname, db_name):
        self.engine = None
        self.session_maker = None
        self.db_username = db_username
        self.db_hostname = db_hostname
        self.db_name = db_name
        self.access_token = None
        self.token_expiration_time = None
        self.token_renewal_margin = timedelta(minutes=5)

    def _get_db_access_token(self, client_id):
        # Authenticate using Managed Identity (MSI)
        try:
            command = ["az", "login", "--identity", "--username", client_id]
            subprocess.check_call(command)
        except subprocess.CalledProcessError as e:
            LOGGER.info("Error during 'az login --identity': {e}")
            raise e

        # Execute Azure CLI command to get the access token
        command = ["az", "account", "get-access-token", "--resource-type", "oss-rdbms"]
        output = subprocess.check_output(command)

        # Parse the output to retrieve the access token and expiration time
        token_info = json.loads(output)
        self.access_token = token_info["accessToken"]
        expires_on_str = token_info["expiresOn"]
        # Convert the expiration time string to a datetime object
        token_expiration_time = datetime.strptime(expires_on_str, "%Y-%m-%d %H:%M:%S.%f")
        self.token_expiration_time = token_expiration_time - self.token_renewal_margin

    def _get_db_connection_string(self):
        # Get the connection string for the PostgreSQL database.

        # Production run, load credentials from the JSON file
        with open('database.json') as f:
            config = json.load(f)

        # Retrieve values from the JSON
        client_id = config["client_id"]  # TODO get this from the Managed Identity name in code

        # Get the password using the client_id from a secure source
        self._get_db_access_token(client_id)

        db_url = f"postgresql://{self.db_username}:{self.access_token}@{self.db_hostname}/{self.db_name}"

        return db_url

    def _get_session(self):
        if self.session_maker is None:
            raise RuntimeError("SessionMaker has not been created. Call create_connection() first.")

        return self.session_maker()

    def create_connection(self):
        try:
            # Create the engine
            db_url = self._get_db_connection_string()
            self.engine = create_engine(db_url)
            self.session_maker = sessionmaker(bind=self.engine)

            LOGGER.info(f"Successfully created database sessionmaker.")

        except SQLAlchemyError as e:
            # Handle any exceptions that occur during connection creation
            LOGGER.info(f"Error creating database sessionmaker: {str(e)}")
            raise e

    @contextmanager
    def managed_session(self):
        # Validate if the token is still valid
        self._validate_token_status()

        session = self._get_session()
        try:
            yield session  # This line yields the 'session' to the with block.
            session.commit()  # Executed when the with block completes
        except SQLAlchemyError as e:
            session.rollback()
            raise e
        finally:
            session.close()

    def close_connection(self):
        try:
            self.engine.dispose()
        except SQLAlchemyError as e:
            LOGGER.info(f"Error disposing the database engine: {str(e)}")
            raise e

    def _validate_token_status(self):
        if self.token_expiration_time < datetime.now():
            # Renew the token
            self._get_db_access_token()
            LOGGER.info("Token for database renewed.")

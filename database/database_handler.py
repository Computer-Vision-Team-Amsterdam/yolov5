from sqlalchemy import create_engine, Column, String, Boolean, Date, Integer, Float
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import SQLAlchemyError

import os
import logging

logger = logging.getLogger(__name__)

LOCAL_RUN = True


def create_connection():
    try:
        # Create the engine
        if LOCAL_RUN:
            db_url = f"postgresql://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}@{os.environ['POSTGRES_HOST']}/{os.environ['POSTGRES_DB']}"
        else:
            db_url = f"postgresql://TODO"
        engine = create_engine(db_url)

        # Create and open a session
        Session = sessionmaker(bind=engine)
        session = Session()

        return engine, session

    except SQLAlchemyError as e:
        # Handle any exceptions that occur during connection creation
        print(f"Error creating database connection: {str(e)}")
        raise

def close_connection(engine, session):
    try:
        # Close the session
        session.close()

        # Dispose the engine
        engine.dispose()

    except SQLAlchemyError as e:
        # Handle any exceptions that occur during connection closing
        print(f"Error closing database connection: {str(e)}")
        raise

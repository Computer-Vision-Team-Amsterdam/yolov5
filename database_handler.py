import psycopg2
import os
import logging

logger = logging.getLogger(__name__)


def create_connection():
    """Create a connection to a PostgreSQL database. """
    try:
        conn = psycopg2.connect(
            host=os.environ['POSTGRES_HOST'],
            database=os.environ['POSTGRES_DB'],
            user=os.environ['POSTGRES_USER'],
            password=os.environ['POSTGRES_PASSWORD']
        )
    except (psycopg2.OperationalError, psycopg2.Error) as e:
        logging.error(f"Error connecting to database: {e}")
        raise e
    except Exception as e:
        logging.error(f"Unknown error: {e}")
        raise e
    else:
        logging.info("Connection to database established successfully!")
        return conn, conn.cursor()


def close_connection(conn, cur):
    """Closes the connection to the database and the associated cursor. """
    logging.info("Closing database connection...")
    cur.close()
    conn.close()

from sqlalchemy import create_engine
import os
import logging

logger = logging.getLogger(__name__)


def create_connection():
    """Create a connection to a PostgreSQL database. """
    try:
        # Construct the database URL
        db_url = f"postgresql://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}@{os.environ['POSTGRES_HOST']}/{os.environ['POSTGRES_DB']}"

        # Create the engine
        engine = create_engine(db_url)

        # Create the connection and cursor
        conn = engine.connect()
        cur = conn.connection.cursor()

    except Exception as e:
        logger.error(f"Error connecting to database: {e}")
        raise e

    else:
        logger.info("Connection to database established successfully!")
        return conn, cur


def close_connection(conn, cur):
    """Closes the connection to the database and the associated cursor. """
    logging.info("Closing database connection...")
    cur.close()
    conn.close()

from sqlalchemy import create_engine, Column, String, Boolean, Date, Integer, Float
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

class ImageProcessingStatus(Base):
    __tablename__ = 'image_processing_status'

    image_customer_name = Column(String, primary_key=True)
    image_upload_date = Column(Date, primary_key=True)
    image_filename = Column(String, primary_key=True)
    processing_status = Column(String)


class DetectionInformation(Base):
    __tablename__ = "detection_information"

    id = Column(Integer, primary_key=True)
    image_customer_name = Column(String)
    image_upload_date = Column(Date)
    image_filename = Column(String)
    has_detection = Column(Boolean)
    class_id = Column(Integer)
    x_norm = Column(Float)
    y_norm = Column(Float)
    w_norm = Column(Float)
    h_norm = Column(Float)
    image_width = Column(Integer)
    image_height = Column(Integer)
    run_id = Column(Integer)
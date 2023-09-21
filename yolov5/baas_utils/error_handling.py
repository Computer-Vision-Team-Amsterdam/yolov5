import os

from .database_handler import DBConfigSQLAlchemy
from .database_tables import BatchRunInformation
from .date_utils import get_current_time


def exception_handler(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            # Handle the exception here
            print(f"Exception caught: {e}")

            db_username = kwargs.get('db_username', '')
            db_name = kwargs.get('db_name', '')
            db_hostname = kwargs.get('db_hostname', '')
            run_id = kwargs.get('run_id', 'default_run_id')
            trained_yolo_model = kwargs.get('weights', 'default')
            start_time = kwargs.get('start_time', '')
            # Check if 'skip_evaluation' is provided as an argument in kwargs, and if not, default to False
            skip_evaluation = kwargs.get('skip_evaluation', False)

            if skip_evaluation:
                # Validate if database credentials are provided
                if not db_username or not db_name or not db_hostname:
                    raise ValueError('Please provide database credentials.')

                # Create a DBConfigSQLAlchemy object
                db_config = DBConfigSQLAlchemy(db_username, db_hostname, db_name)
                # Create the database connection
                db_config.create_connection()

                # Perform database operations using the 'session'
                # The session will be automatically closed at the end of this block
                with db_config.managed_session() as session:
                    # Create an instance of BatchRunInformation
                    batch_info = BatchRunInformation(run_id=run_id,
                                                     start_time=start_time,
                                                     end_time=get_current_time(),
                                                     trained_yolo_model=os.path.split(trained_yolo_model)[-1],
                                                     success=False,
                                                     error_code=str(e))

                    # Add the instance to the session
                    session.add(batch_info)

            # Re-raise the exception
            raise e
    return wrapper

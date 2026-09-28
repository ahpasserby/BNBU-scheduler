"""Campus print portal and private, on-device execution agent."""


def create_print_blueprint(db_path):
    from .api import create_blueprint
    return create_blueprint(db_path)

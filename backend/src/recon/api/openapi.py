"""Write the API description the frontend types are generated from.

python -m recon.api.openapi > ../frontend/openapi.json
"""

import json
import sys

from recon.config import Settings
from recon.main import create_app


def main() -> None:
    # Building the app does not connect to the database, so no real URL is needed.
    app = create_app(
        Settings(database_url="postgresql+psycopg://unused:unused@localhost/unused", demo=True)
    )
    json.dump(app.openapi(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

from embeddr.db.session import create_db_and_tables
import sys
from pathlib import Path

# Add parent directory to path
current_dir = Path(__file__).resolve().parent
parent_dir = current_dir.parent
sys.path.append(str(parent_dir))

# Import models to register them

if __name__ == "__main__":
    print("Creating tables...")
    create_db_and_tables()
    print("Tables created.")

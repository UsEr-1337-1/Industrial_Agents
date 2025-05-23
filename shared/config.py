import os
from dotenv import load_dotenv

# Charger les variables d'environnement depuis un fichier .env
# Utile pour ne pas mettre d'informations sensibles dans le code
dotenv_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env') # Chemin vers .env à la racine du projet
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)
else:
    print("Avertissement: Fichier .env non trouvé. Utilisation des valeurs par défaut ou des variables d'environnement système.")

# Configuration de la base de données
# Pour commencer, nous utiliserons SQLite, qui crée un fichier local.
# Parfait pour le développement.
DEFAULT_DATABASE_URL = "sqlite:///./usine_data.db" # Crée un fichier usine_data.db à la racine
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)

# Autres configurations possibles
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Configuration pour le bus de messages (exemples, à adapter si vous utilisez RabbitMQ/Redis)
# MESSAGE_BROKER_URL = os.getenv("MESSAGE_BROKER_URL", "amqp://guest:guest@localhost:5672/") # RabbitMQ
# MESSAGE_BROKER_URL = os.getenv("MESSAGE_BROKER_URL", "redis://localhost:6379/0") # Redis

if __name__ == '__main__':
    print(f"URL de la base de données : {DATABASE_URL}")
    print(f"Niveau de log : {LOG_LEVEL}")
import logging
import asyncio
import signal
from typing import List, Optional
import threading  # Ajout pour le thread Flask

from shared.config import LOG_LEVEL
from shared.utils import create_db_and_tables, get_db_session, initialize_sample_data
from db.models import ProductionOrder as ProductionOrderModelDb, Part as PartModelDb, Machine as MachineModelDb
from agents.communication import InMemoryCommunicationChannel, Message
from agents.agent_machine import MachineAgent
from agents.agent_stock import StockAgent
from planning.planner import ProductionPlanner
from dashboard.app import app as flask_app, FlaskServerThread  # <-- Importer l'app Flask et le Thread

logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s - %(levelname)-8s - %(name)-25s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

agent_tasks: List[asyncio.Task] = []
keep_main_loop_running = True
flask_thread: Optional[FlaskServerThread] = None  # Pour garder une référence au thread Flask


async def main_async_logic():
    global keep_main_loop_running, agent_tasks, flask_thread  # Ajout de flask_thread
    logger.info("Démarrage de la logique asynchrone du système...")

    comm_channel = InMemoryCommunicationChannel()

    db_for_setup_gen = get_db_session()
    db_for_setup = next(db_for_setup_gen)

    machine_models_from_db = []
    try:
        machine_models_from_db = db_for_setup.query(MachineModelDb).all()
        if not machine_models_from_db:
            logger.warning(
                "Aucune machine trouvée en DB pour créer les agents machines. Vérifiez l'initialisation des données de test.")
    except Exception as e:
        logger.error(f"Erreur lors de la récupération des machines en DB: {e}")
    finally:
        db_for_setup.close()

    machine_agents: List[MachineAgent] = []
    for machine_model in machine_models_from_db:
        agent = MachineAgent(
            machine_id_db=machine_model.id,
            communication_channel=comm_channel,
            db_session_factory=get_db_session
        )
        machine_agents.append(agent)

    stock_agent = StockAgent(
        agent_id="stock_agent_main",
        communication_channel=comm_channel,
        db_session_factory=get_db_session
    )

    planner = ProductionPlanner(
        agent_id="production_planner_main",
        communication_channel=comm_channel,
        db_session_factory=get_db_session
    )

    # Démarrer le serveur Flask dans un thread séparé AVANT de lancer les tâches asyncio bloquantes
    # pour s'assurer qu'il est prêt à recevoir des requêtes si les agents démarrent très vite.
    # Cependant, les agents accèdent à la DB, et Flask aussi. get_db_session() devrait gérer des sessions distinctes.
    flask_thread = FlaskServerThread(flask_app, host='0.0.0.0', port=5000)
    flask_thread.start()

    logger.info("Démarrage des tâches des agents et du planificateur...")
    all_components_to_run = [stock_agent, planner] + machine_agents

    for component in all_components_to_run:
        if hasattr(component, 'run') and callable(component.run):
            task_name = component.agent_id if hasattr(component,
                                                      'agent_id') else f"Task-UnknownComponent-{id(component)}"
            agent_tasks.append(asyncio.create_task(component.run(), name=task_name))
        else:
            logger.warning(f"Composant {type(component)} n'a pas de méthode 'run' ou elle n'est pas callable.")

    try:
        while keep_main_loop_running:
            all_done = True
            for task in agent_tasks:
                if not task.done():
                    all_done = False
                    break
            if all_done and agent_tasks:
                logger.info("Toutes les tâches des agents semblent terminées. Arrêt de la boucle principale.")
                keep_main_loop_running = False

            await asyncio.sleep(1)
    except asyncio.CancelledError:
        logger.info("Boucle principale asyncio annulée.")

    logger.info("Signal d'arrêt reçu ou toutes les tâches terminées. Arrêt des composants...")
    for component in all_components_to_run:
        if hasattr(component, 'stop') and callable(component.stop):
            component.stop()

    if flask_thread:  # Arrêter le thread Flask
        logger.info("Demande d'arrêt du serveur Flask...")
        flask_thread.shutdown()  # Demander au serveur de s'arrêter
        flask_thread.join(timeout=5)  # Attendre que le thread se termine
        if flask_thread.is_alive():
            logger.warning("Le thread Flask n'a pas pu s'arrêter proprement dans le délai imparti.")

    logger.info("Attente de la terminaison des tâches des agents (max 10 secondes)...")
    if agent_tasks:  # S'assurer qu'il y a des tâches à attendre
        done, pending = await asyncio.wait(agent_tasks, timeout=10.0, return_when=asyncio.ALL_COMPLETED)

        for task in pending:
            task_name_pending = task.get_name() if hasattr(task, 'get_name') else "UnknownTask"
            logger.warning(f"La tâche {task_name_pending} n'a pas pu s'arrêter à temps et va être annulée.")
            task.cancel()

        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    logger.info("Toutes les tâches des agents/planificateur sont terminées ou annulées.")


def signal_handler(signum, frame):
    global keep_main_loop_running
    logger.info(f"Signal d'arrêt reçu ({signal.Signals(signum).name}).")
    if keep_main_loop_running:
        keep_main_loop_running = False
    else:
        logger.warning("Signal d'arrêt reçu pendant l'arrêt. Forcer la sortie si nécessaire.")
        # On pourrait forcer l'arrêt des threads/tâches ici plus agressivement si besoin


def main_setup_sync():
    logger.info("Démarrage de la configuration initiale du système (synchrone)...")
    try:
        create_db_and_tables()
        db_session_generator = get_db_session()
        db = next(db_session_generator)
        try:
            initialize_sample_data(db)
            orders_in_db_count = db.query(ProductionOrderModelDb).count()  # Utiliser le nom importé
            logger.info(f"Nombre d'ordres de production après initialisation : {orders_in_db_count}")
        except Exception as e_init:
            logger.error(f"Erreur lors de l'initialisation des données: {e_init}", exc_info=True)
            if db.is_active: db.rollback()
        finally:
            if db.is_active: db.close()
    except Exception as e_setup:
        logger.error(f"Erreur majeure lors de la configuration initiale de la DB : {e_setup}", exc_info=True)
        raise
    logger.info("Configuration initiale (synchrone) terminée.")


if __name__ == "__main__":
    main_setup_sync()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    flask_server_instance = None  # Pour pouvoir y accéder dans finally
    try:
        logger.info("Lancement de la boucle d'événements asyncio...")
        asyncio.run(main_async_logic())
    except KeyboardInterrupt:
        logger.info("Interruption clavier (devrait être gérée par le signal handler).")
    except asyncio.CancelledError:
        logger.info("Boucle d'événements principale asyncio annulée.")
    finally:
        logger.info("Système principal de l'usine arrêté.")
        # S'assurer que le thread Flask est bien arrêté s'il n'a pas été arrêté dans main_async_logic
        # Ceci est une sécurité supplémentaire, normalement flask_thread.shutdown() dans main_async_logic devrait suffire
        if flask_thread and flask_thread.is_alive():
            logger.info("Nettoyage final: Tentative d'arrêt du thread Flask...")
            flask_thread.shutdown()
            flask_thread.join(timeout=2)
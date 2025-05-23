import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, List
import logging
import uuid  # Pour des ID de messages uniques
import time  # Utilisé pour le timestamp par défaut si la boucle asyncio n'est pas encore active

logger = logging.getLogger(__name__)

# Types de messages
MSG_TYPE_PART_REQUEST = "PART_REQUEST"
MSG_TYPE_PART_RESPONSE = "PART_RESPONSE"
MSG_TYPE_STOCK_UPDATE_REQUEST = "STOCK_UPDATE_REQUEST"
MSG_TYPE_STATUS_UPDATE = "STATUS_UPDATE"
MSG_TYPE_PLANNING_INSTRUCTION = "PLANNING_INSTRUCTION"
MSG_TYPE_MACHINE_ERROR = "MACHINE_ERROR"


def default_timestamp():
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:  # Aucune boucle d'événements en cours
        return time.time()


@dataclass
class Message:
    """Classe de base pour tous les messages échangés."""
    # Champs sans valeur par défaut en premier
    sender_id: str
    message_type: str

    # Champs avec valeur par défaut ensuite
    payload: Dict[str, Any] = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    receiver_id: Optional[str] = None  # Optionnel, donc peut avoir une valeur par défaut (None)
    timestamp: float = field(default_factory=default_timestamp)

    def __repr__(self):
        return (f"Message(id={self.message_id}, type='{self.message_type}', "
                f"sender='{self.sender_id}', receiver='{self.receiver_id}', "
                f"payload_keys='{list(self.payload.keys())}')")


# --- Exemples de structures de payload spécifiques pour chaque type de message ---
# Payload pour MSG_TYPE_PART_REQUEST
# payload = {"part_id": int, "quantity_requested": int, "production_order_id": Optional[int]}

# Payload pour MSG_TYPE_PART_RESPONSE
# payload = {"part_id": int, "quantity_provided": int, "available": bool, "request_message_id": str}

# Payload pour MSG_TYPE_STATUS_UPDATE (ex: MachineAgent)
# payload = {"machine_db_id": int, "status": str, "current_production_order_id": Optional[int], "details": Optional[str]}

# Payload pour MSG_TYPE_PLANNING_INSTRUCTION
# payload = {"production_order_id": int, "action": "START" / "STOP"}

# Payload pour MSG_TYPE_MACHINE_ERROR
# payload = {"error_code": Optional[str], "description": str}


class InMemoryCommunicationChannel:
    """
    Un canal de communication en mémoire très simple pour simuler un bus de messages.
    Utilise des files d'attente asyncio pour la communication entre coroutines.
    """

    def __init__(self):
        self._agent_mailboxes: Dict[str, asyncio.Queue[Message]] = {}  # Spécifier le type Message pour la Queue
        self._topics: Dict[str, List[asyncio.Queue[Message]]] = {}
        self._lock = asyncio.Lock()
        logger.info("Canal de communication en mémoire initialisé.")

    async def register_agent(self, agent_id: str) -> asyncio.Queue[Message]:
        async with self._lock:
            if agent_id not in self._agent_mailboxes:
                self._agent_mailboxes[agent_id] = asyncio.Queue()
                logger.info(f"Agent '{agent_id}' enregistré et boîte aux lettres créée.")
            return self._agent_mailboxes[agent_id]

    async def subscribe_to_topic(self, topic: str, queue: asyncio.Queue[Message]):
        async with self._lock:
            if topic not in self._topics:
                self._topics[topic] = []
            if queue not in self._topics[topic]:
                self._topics[topic].append(queue)
                logger.info(f"File d'attente {queue} abonnée au sujet '{topic}'.")

    async def send_direct_message(self, message: Message):
        if not message.receiver_id:
            logger.error(f"Tentative d'envoi de message direct sans receiver_id: {message}")
            return

        queue: Optional[asyncio.Queue[Message]] = None
        async with self._lock:  # Protéger la lecture du dictionnaire
            queue = self._agent_mailboxes.get(message.receiver_id)

        if queue:
            await queue.put(message)
            # logger.debug(f"Message direct envoyé de '{message.sender_id}' à '{message.receiver_id}': {message.message_type}")
        else:
            logger.warning(
                f"Aucune boîte aux lettres trouvée pour le destinataire '{message.receiver_id}'. Message de '{message.sender_id}' non délivré: {message.message_type}")

    async def publish_to_topic(self, topic: str, message: Message):
        queues_to_notify: List[asyncio.Queue[Message]] = []
        async with self._lock:
            if topic in self._topics:
                # Copier la liste pour éviter les problèmes si la liste est modifiée pendant l'itération (peu probable ici mais bonne pratique)
                queues_to_notify.extend(list(self._topics[topic]))

        if not queues_to_notify:
            logger.debug(
                f"Aucun abonné pour le sujet '{topic}'. Message de '{message.sender_id}' non diffusé via ce sujet.")
            return

        # logger.debug(f"Publication du message de '{message.sender_id}' sur le sujet '{topic}' à {len(queues_to_notify)} abonnés.")
        # Créer des copies du message pour chaque file si les consommateurs peuvent modifier le message.
        # Pour les dataclasses simples, ce n'est souvent pas un problème si utilisées de manière immuable.
        for queue in queues_to_notify:
            try:
                # On envoie le même objet message, en supposant qu'il n'est pas modifié par les consommateurs.
                # Si la modification est un souci, il faudrait faire une copie (ex: dataclasses.replace)
                await queue.put(message)
            except Exception as e:
                logger.error(f"Erreur lors de la mise en file d'attente pour le sujet '{topic}' vers {queue}: {e}")
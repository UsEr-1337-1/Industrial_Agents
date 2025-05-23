import asyncio
import logging
from typing import Dict, Optional
from sqlalchemy.orm import Session

from agents.communication import Message, InMemoryCommunicationChannel, MSG_TYPE_PART_REQUEST, MSG_TYPE_PART_RESPONSE, MSG_TYPE_STOCK_UPDATE_REQUEST
from db.models import Part as PartModel # Renommer Part pour éviter conflit
from shared.utils import get_db_session

logger = logging.getLogger(__name__)

class StockAgent:
    def __init__(self, agent_id: str, communication_channel: InMemoryCommunicationChannel, db_session_factory):
        self.agent_id = agent_id
        self.communication_channel = communication_channel
        self.db_session_factory = db_session_factory # Fonction pour obtenir une session DB

        self.inventory: Dict[int, int] = {} # part_id (DB): quantity

        self._keep_running = True
        self.message_queue: Optional[asyncio.Queue] = None

        logger.info(f"Agent de Stock {self.agent_id} initialisé.")

    async def register_with_comm_channel(self):
        self.message_queue = await self.communication_channel.register_agent(self.agent_id)
        logger.info(f"Agent {self.agent_id} enregistré sur le canal de communication.")
        # S'abonner aux demandes de mise à jour de stock si nécessaire
        # await self.communication_channel.subscribe_to_topic("stock_updates", self.message_queue)

    def _get_db_session(self) -> Session:
        gen = self.db_session_factory()
        return next(gen)

    async def load_inventory_from_db(self):
        """Charge l'inventaire initial depuis la base de données."""
        db = self._get_db_session()
        try:
            parts = db.query(PartModel).all()
            for part in parts:
                self.inventory[part.id] = part.current_stock
            logger.info(f"Agent {self.agent_id}: Inventaire chargé depuis la DB. {len(self.inventory)} types de pièces.")
        finally:
            db.close()

    async def _update_stock_in_db(self, part_id: int, new_quantity: int):
        """Met à jour le stock d'une pièce dans la DB."""
        db = self._get_db_session()
        try:
            part_model = db.query(PartModel).filter(PartModel.id == part_id).first()
            if part_model:
                part_model.current_stock = new_quantity
                db.commit()
                logger.debug(f"Agent {self.agent_id}: Stock de la pièce ID {part_id} mis à jour en DB -> {new_quantity}")
            else:
                logger.error(f"Agent {self.agent_id}: Impossible de mettre à jour le stock, pièce ID {part_id} non trouvée en DB.")
        except Exception as e:
            logger.error(f"Agent {self.agent_id}: Erreur lors de la mise à jour du stock en DB pour pièce {part_id}: {e}")
            db.rollback()
        finally:
            db.close()


    async def handle_part_request(self, request_message: Message):
        """Gère une demande de pièce."""
        payload = request_message.payload
        part_id = payload.get("part_id")
        quantity_requested = payload.get("quantity_requested", 0)
        requester_agent_id = request_message.sender_id

        logger.info(f"Agent {self.agent_id}: Reçu demande de {quantity_requested} pièce(s) ID {part_id} de {requester_agent_id}.")

        available = False
        quantity_provided = 0

        if part_id in self.inventory and self.inventory[part_id] >= quantity_requested:
            self.inventory[part_id] -= quantity_requested
            quantity_provided = quantity_requested
            available = True
            await self._update_stock_in_db(part_id, self.inventory[part_id]) # Mettre à jour la DB
            logger.info(f"Agent {self.agent_id}: {quantity_provided} pièce(s) ID {part_id} fournies à {requester_agent_id}. Stock restant: {self.inventory[part_id]}")
        else:
            logger.warning(f"Agent {self.agent_id}: Stock insuffisant pour pièce ID {part_id} (demandé: {quantity_requested}, dispo: {self.inventory.get(part_id, 0)}).")

        response_payload = {
            "part_id": part_id,
            "quantity_provided": quantity_provided,
            "available": available,
            "request_message_id": request_message.message_id # Pour que le demandeur puisse lier la réponse à sa demande
        }
        response_msg = Message(
            sender_id=self.agent_id,
            receiver_id=requester_agent_id,
            message_type=MSG_TYPE_PART_RESPONSE,
            payload=response_payload
        )
        await self.communication_channel.send_direct_message(response_msg)

    async def handle_incoming_message(self, message: Message):
        """Traite un message reçu."""
        logger.debug(f"Agent {self.agent_id} a reçu un message: {message}")
        if message.message_type == MSG_TYPE_PART_REQUEST:
            await self.handle_part_request(message)
        elif message.message_type == MSG_TYPE_STOCK_UPDATE_REQUEST: # Ex: un admin ajoute du stock
            part_id = message.payload.get("part_id")
            quantity_change = message.payload.get("quantity_change", 0) # Peut être positif ou négatif
            new_stock_value = self.inventory.get(part_id, 0) + quantity_change
            if new_stock_value < 0: new_stock_value = 0 # Ne pas avoir de stock négatif

            self.inventory[part_id] = new_stock_value
            await self._update_stock_in_db(part_id, new_stock_value)
            logger.info(f"Agent {self.agent_id}: Stock pour pièce ID {part_id} mis à jour par demande externe. Nouveau stock: {new_stock_value}")

        # Ajouter d'autres logiques de messages

    async def run(self):
        """Boucle principale de l'agent."""
        await self.register_with_comm_channel()
        await self.load_inventory_from_db()

        while self._keep_running:
            try:
                message = await asyncio.wait_for(self.message_queue.get(), timeout=1.0)
                await self.handle_incoming_message(message)
                self.message_queue.task_done()
            except asyncio.TimeoutError:
                pass # Pas de message, continuer
            except Exception as e:
                logger.error(f"Agent {self.agent_id}: Erreur dans la boucle run(): {e}")
                await asyncio.sleep(5) # Pause en cas d'erreur

            await asyncio.sleep(0.1)

        logger.info(f"Agent {self.agent_id} arrêté.")

    def stop(self):
        self._keep_running = False
        logger.info(f"Demande d'arrêt pour l'agent {self.agent_id}.")
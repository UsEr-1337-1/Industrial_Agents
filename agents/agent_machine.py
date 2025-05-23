# agents/agent_machine.py (Fichier Complet)

import asyncio
import logging
from typing import Dict, Any, Optional, List
from sqlalchemy.orm import Session
from sqlalchemy.sql import func  # Pour func.now() si on décide de l'utiliser pour completed_at
from datetime import datetime, timezone  # Pour datetime.now(timezone.utc)

from agents.communication import Message, InMemoryCommunicationChannel, MSG_TYPE_PART_RESPONSE, \
    MSG_TYPE_PLANNING_INSTRUCTION, MSG_TYPE_PART_REQUEST, MSG_TYPE_STATUS_UPDATE, MSG_TYPE_MACHINE_ERROR
from db.models import Machine as MachineModel, Part as PartModel, MachineStatusEnum, \
    ProductionOrder as ProductionOrderModel, OrderPartLink, OrderStatusEnum, ResourceUsageLog
from shared.utils import get_db_session

logger = logging.getLogger(__name__)


class MachineAgent:
    def __init__(self, machine_id_db: int, communication_channel: InMemoryCommunicationChannel, db_session_factory):
        self.agent_id = f"machine_agent_{machine_id_db}"
        self.machine_id_db = machine_id_db
        self.communication_channel = communication_channel
        self.db_session_factory = db_session_factory

        self.status: MachineStatusEnum = MachineStatusEnum.IDLE
        self.current_parts_stock: Dict[int, int] = {}
        self.current_production_order_id: Optional[int] = None

        self._keep_running = True
        self.message_queue: Optional[asyncio.Queue[Message]] = None

        logger.info(f"Agent Machine {self.agent_id} (pour DB ID {self.machine_id_db}) initialisé.")

    async def register_with_comm_channel(self):
        self.message_queue = await self.communication_channel.register_agent(self.agent_id)
        logger.info(f"Agent {self.agent_id} enregistré sur le canal de communication.")

    def _get_db_session(self) -> Session:
        gen = self.db_session_factory()
        return next(gen)

    async def load_initial_state_from_db(self):
        db = self._get_db_session()
        try:
            machine_model = db.query(MachineModel).filter(MachineModel.id == self.machine_id_db).first()
            if machine_model:
                self.status = machine_model.status
                if self.status != MachineStatusEnum.IDLE:
                    logger.info(
                        f"Agent {self.agent_id}: Machine trouvée avec statut {self.status}. Réinitialisation à IDLE pour le démarrage.")
                    self.status = MachineStatusEnum.IDLE
                    machine_model.status = MachineStatusEnum.IDLE
                    machine_model.current_production_order_id = None
                    db.commit()
                self.current_production_order_id = None
                logger.info(
                    f"Agent {self.agent_id}: État initial chargé/réinitialisé depuis la DB: status={self.status}")
            else:
                logger.error(f"Agent {self.agent_id}: Machine avec DB ID {self.machine_id_db} non trouvée en DB.")
                self.status = MachineStatusEnum.ERROR
        finally:
            db.close()

    async def _update_status_in_db(self, new_status: MachineStatusEnum, production_order_id: Optional[int] = None,
                                   commit_session: bool = True):
        current_order_for_machine = production_order_id
        # Si production_order_id n'est pas explicitement fourni, on essaie de garder l'actuel SAUF si le nouveau statut l'invalide.
        if production_order_id is None and new_status not in [MachineStatusEnum.IDLE, MachineStatusEnum.ERROR,
                                                              MachineStatusEnum.MAINTENANCE]:
            current_order_for_machine = self.current_production_order_id  # Conserver l'ordre en cours
        elif new_status in [MachineStatusEnum.IDLE, MachineStatusEnum.ERROR, MachineStatusEnum.MAINTENANCE]:
            current_order_for_machine = None

        db = self._get_db_session()
        try:
            machine_model = db.query(MachineModel).filter(MachineModel.id == self.machine_id_db).first()
            if machine_model:
                machine_model.status = new_status
                machine_model.current_production_order_id = current_order_for_machine
                if commit_session:
                    db.commit()
                self.status = new_status
                self.current_production_order_id = current_order_for_machine
                logger.debug(
                    f"Agent {self.agent_id}: Statut mis à jour en DB -> {new_status}, order_id={current_order_for_machine}")
            else:
                logger.error(
                    f"Agent {self.agent_id}: Impossible de mettre à jour le statut, machine DB ID {self.machine_id_db} non trouvée.")

            if not commit_session:  # Si on ne commit pas, on retourne la session pour un commit externe
                return db
        except Exception as e:
            logger.error(f"Agent {self.agent_id}: Erreur lors de la mise à jour du statut en DB: {e}")
            if commit_session and db.is_active:
                db.rollback()
            # Si on ne commit pas et qu'il y a une erreur, la session sera fermée sans commit
        finally:
            if commit_session or not db.is_active:
                db.close()

    async def send_status_update(self, details: Optional[str] = None):
        payload = {
            "machine_db_id": self.machine_id_db,
            "status": self.status.value,
            "current_production_order_id": self.current_production_order_id,
        }
        if details:
            payload["details"] = details

        status_message = Message(
            sender_id=self.agent_id,
            message_type=MSG_TYPE_STATUS_UPDATE,
            payload=payload
        )
        await self.communication_channel.publish_to_topic("machine_status_updates", status_message)
        logger.info(
            f"Agent {self.agent_id}: Statut publié: {self.status.value} (Order: {self.current_production_order_id or 'None'}) {details or ''}")

    async def request_part(self, part_db_id: int, quantity: int, stock_agent_id: str):
        logger.info(
            f"Agent {self.agent_id} demande {quantity} de la pièce ID {part_db_id} à {stock_agent_id} pour ordre {self.current_production_order_id}.")
        payload = {
            "part_id": part_db_id,
            "quantity_requested": quantity,
            "production_order_id": self.current_production_order_id
        }
        request_msg = Message(
            sender_id=self.agent_id,
            receiver_id=stock_agent_id,
            message_type=MSG_TYPE_PART_REQUEST,
            payload=payload
        )
        await self.communication_channel.send_direct_message(request_msg)

    async def handle_incoming_message(self, message: Message):
        logger.debug(f"Agent {self.agent_id} a reçu un message: {message}")
        if message.message_type == MSG_TYPE_PART_RESPONSE:
            part_id = message.payload.get("part_id")
            qty_provided = message.payload.get("quantity_provided", 0)
            available = message.payload.get("available", False)

            if available:
                self.current_parts_stock[part_id] = self.current_parts_stock.get(part_id, 0) + qty_provided
                logger.info(
                    f"Agent {self.agent_id}: Pièces ID {part_id} ({qty_provided}) reçues. Stock local: {self.current_parts_stock[part_id]}")
                await self.check_parts_and_start_work()
            else:
                logger.warning(f"Agent {self.agent_id}: Demande de pièce ID {part_id} REFUSÉE ou non disponible.")
                await self._update_status_in_db(MachineStatusEnum.ERROR, self.current_production_order_id)
                await self.send_status_update(
                    f"Pièce ID {part_id} non disponible pour ordre {self.current_production_order_id}")

        elif message.message_type == MSG_TYPE_PLANNING_INSTRUCTION:
            order_id = message.payload.get("production_order_id")
            action = message.payload.get("action", "").upper()
            logger.info(f"Agent {self.agent_id}: Instruction de planification reçue pour ordre {order_id} -> {action}")

            if action == "START":
                if self.status == MachineStatusEnum.IDLE:
                    self.current_production_order_id = order_id
                    await self._update_status_in_db(MachineStatusEnum.WORKING, order_id)
                    await self.send_status_update("Instruction START reçue, passage en WORKING.")
                    await self.request_required_parts_for_current_order()
                else:
                    logger.warning(
                        f"Agent {self.agent_id}: Reçu START pour ordre {order_id} mais statut actuel: {self.status}. Instruction ignorée.")
            elif action == "STOP":
                if self.current_production_order_id == order_id:
                    logger.info(f"Agent {self.agent_id}: Instruction STOP reçue pour ordre en cours {order_id}.")
                    db_session = await self._update_status_in_db(MachineStatusEnum.IDLE, None, commit_session=False)
                    try:
                        order_model = db_session.query(ProductionOrderModel).filter(
                            ProductionOrderModel.id == order_id).first()
                        if order_model and order_model.status not in [OrderStatusEnum.COMPLETED,
                                                                      OrderStatusEnum.CANCELLED]:
                            order_model.status = OrderStatusEnum.CANCELLED
                            logger.info(f"Agent {self.agent_id}: Ordre {order_id} marqué comme annulé.")
                        db_session.commit()
                    except Exception as e:
                        logger.error(f"Erreur MAJ statut ordre {order_id} en CANCELLED: {e}", exc_info=True)
                        if db_session.is_active: db_session.rollback()
                    finally:
                        if db_session.is_active: db_session.close()
                    await self.send_status_update(f"Ordre {order_id} arrêté/annulé.")
                else:
                    logger.warning(
                        f"Agent {self.agent_id}: Reçu STOP pour ordre {order_id} mais pas l'ordre en cours ({self.current_production_order_id}).")

    async def request_required_parts_for_current_order(self):
        if self.current_production_order_id is None:
            logger.warning(f"Agent {self.agent_id}: Aucune commande en cours pour demander des pièces.")
            return

        db = self._get_db_session()
        try:
            part_links: List[OrderPartLink] = (
                db.query(OrderPartLink)
                .filter(OrderPartLink.production_order_id == self.current_production_order_id)
                .all()
            )
            if not part_links:
                logger.info(
                    f"Agent {self.agent_id}: Aucune pièce requise trouvée pour l'ordre ID {self.current_production_order_id}. Prêt à 'travailler'.")
                await self.check_parts_and_start_work()
                return

            logger.info(f"Agent {self.agent_id}: Pièces requises pour ordre {self.current_production_order_id}:")
            for link in part_links:
                part_id_to_request = link.part_id
                quantity_to_request = link.quantity_required
                logger.info(f"  - Demande pour Pièce ID {part_id_to_request}, Quantité: {quantity_to_request}")
                await self.request_part(part_db_id=part_id_to_request, quantity=quantity_to_request,
                                        stock_agent_id="stock_agent_main")
        except Exception as e:
            logger.error(
                f"Agent {self.agent_id}: Erreur lors de la récupération/demande des pièces pour l'ordre {self.current_production_order_id}: {e}",
                exc_info=True)
            await self._update_status_in_db(MachineStatusEnum.ERROR, self.current_production_order_id)
            await self.send_status_update("Erreur demande pièces ordre.")
        finally:
            db.close()

    async def check_parts_and_start_work(self):
        if self.current_production_order_id is None or self.status != MachineStatusEnum.WORKING:
            return

        db = self._get_db_session()
        try:
            part_links: List[OrderPartLink] = (
                db.query(OrderPartLink)
                .filter(OrderPartLink.production_order_id == self.current_production_order_id)
                .all()
            )
            order_model = db.query(ProductionOrderModel).filter(
                ProductionOrderModel.id == self.current_production_order_id).first()

            if not order_model:
                logger.error(
                    f"Agent {self.agent_id}: Ordre {self.current_production_order_id} non trouvé en DB lors de check_parts_and_start_work.")
                await self._update_status_in_db(MachineStatusEnum.ERROR, self.current_production_order_id)
                await self.send_status_update(f"Erreur critique: Ordre {self.current_production_order_id} non trouvé.")
                return

            if not part_links:
                logger.info(
                    f"Agent {self.agent_id}: Aucune pièce spécifiée pour l'ordre {self.current_production_order_id}. Passage à ACTIVE_WORKING.")
                if order_model.status == OrderStatusEnum.IN_PROGRESS:
                    order_model.status = OrderStatusEnum.ACTIVE_WORKING
                    db.commit()
                    logger.info(
                        f"Agent {self.agent_id}: Ordre {self.current_production_order_id} passé à ACTIVE_WORKING (aucune pièce requise).")
                return

            all_parts_available = True
            for link in part_links:
                if self.current_parts_stock.get(link.part_id, 0) < link.quantity_required:
                    all_parts_available = False
                    logger.info(
                        f"Agent {self.agent_id}: Pièce ID {link.part_id} manquante pour ordre {self.current_production_order_id} (besoin: {link.quantity_required}, dispo local: {self.current_parts_stock.get(link.part_id, 0)}). En attente...")
                    break

            if all_parts_available:
                logger.info(
                    f"Agent {self.agent_id}: Toutes les pièces sont disponibles localement pour l'ordre {self.current_production_order_id}. Passage à ACTIVE_WORKING.")
                if order_model.status == OrderStatusEnum.IN_PROGRESS:
                    order_model.status = OrderStatusEnum.ACTIVE_WORKING
                    db.commit()
                    logger.info(
                        f"Agent {self.agent_id}: Ordre {self.current_production_order_id} passé à ACTIVE_WORKING.")
        except Exception as e:
            logger.error(
                f"Agent {self.agent_id}: Erreur lors de check_parts_and_start_work pour ordre {self.current_production_order_id}: {e}",
                exc_info=True)
            if db.is_active: db.rollback()
        finally:
            db.close()

    async def simulate_work_cycle(self):
        if self.current_production_order_id is None or self.status != MachineStatusEnum.WORKING:
            return  # Ne rien faire si pas d'ordre ou si pas dans l'état WORKING général

        db_for_check = self._get_db_session()
        order_model_check = None
        try:
            order_model_check = db_for_check.query(ProductionOrderModel).filter(
                ProductionOrderModel.id == self.current_production_order_id).first()
            if not order_model_check or order_model_check.status != OrderStatusEnum.ACTIVE_WORKING:
                # logger.debug(f"Agent {self.agent_id}: Ordre {self.current_production_order_id} pas encore en ACTIVE_WORKING. Statut DB: {order_model_check.status if order_model_check else 'Non trouvé'}. Pas de travail effectif.")
                return
        finally:
            db_for_check.close()

        logger.info(
            f"Agent {self.agent_id}: **Commence le TRAVAIL ACTIF** sur l'ordre {self.current_production_order_id}...")
        await asyncio.sleep(10)

        db_session_for_completion = self._get_db_session()
        try:
            part_links_for_consumption: List[OrderPartLink] = (
                db_session_for_completion.query(OrderPartLink)
                .filter(OrderPartLink.production_order_id == self.current_production_order_id)
                .all()
            )
            for link in part_links_for_consumption:
                if self.current_parts_stock.get(link.part_id, 0) >= link.quantity_required:
                    self.current_parts_stock[link.part_id] -= link.quantity_required
                    logger.info(
                        f"Agent {self.agent_id}: Pièce ID {link.part_id} ({link.quantity_required}) consommée pour ordre {self.current_production_order_id}. Stock local restant: {self.current_parts_stock[link.part_id]}")
                    usage_log = ResourceUsageLog(
                        machine_id=self.machine_id_db,
                        part_id=link.part_id,
                        production_order_id=self.current_production_order_id,
                        quantity_used=link.quantity_required,
                        timestamp=datetime.now(timezone.utc)  # Utiliser un timestamp UTC cohérent
                    )
                    db_session_for_completion.add(usage_log)
                else:
                    logger.error(
                        f"Agent {self.agent_id}: INCONSISTANCE! Pièce ID {link.part_id} non dispo pour consommation (besoin: {link.quantity_required}, stock: {self.current_parts_stock.get(link.part_id, 0)}). Ordre {self.current_production_order_id}")
                    await self._update_status_in_db(MachineStatusEnum.ERROR, self.current_production_order_id)
                    await self.send_status_update(
                        f"Erreur de stock interne pendant la consommation pour ordre {self.current_production_order_id}.")
                    if db_session_for_completion.is_active: db_session_for_completion.rollback()
                    return

            order_model_to_complete = db_session_for_completion.query(ProductionOrderModel).filter(
                ProductionOrderModel.id == self.current_production_order_id).first()
            if order_model_to_complete:
                order_model_to_complete.status = OrderStatusEnum.COMPLETED
                order_model_to_complete.completed_at = datetime.now(
                    timezone.utc)  # <-- ENREGISTRER L'HEURE D'ACHÈVEMENT
                logger.info(
                    f"Agent {self.agent_id}: Ordre {self.current_production_order_id} marqué comme COMPLETED en DB à {order_model_to_complete.completed_at}.")

            db_session_for_completion.commit()

            completed_order_id = self.current_production_order_id
            self.current_parts_stock.clear()  # Vider le stock local de la machine une fois la tâche finie
            await self._update_status_in_db(MachineStatusEnum.IDLE, None)
            logger.info(f"Agent {self.agent_id}: Tâche pour l'ordre {completed_order_id} terminée.")
            await self.send_status_update(f"Tâche pour ordre {completed_order_id} terminée.")

        except Exception as e:
            logger.error(
                f"Agent {self.agent_id}: Erreur pendant la consommation/finalisation de l'ordre {self.current_production_order_id}: {e}",
                exc_info=True)
            if db_session_for_completion.is_active: db_session_for_completion.rollback()
            await self._update_status_in_db(MachineStatusEnum.ERROR, self.current_production_order_id)
            await self.send_status_update(f"Erreur de processing pour ordre {self.current_production_order_id}.")
        finally:
            if db_session_for_completion.is_active: db_session_for_completion.close()

    async def run(self):
        await self.register_with_comm_channel()
        await self.load_initial_state_from_db()
        await self.send_status_update("Agent démarré")

        while self._keep_running:
            try:
                message = await asyncio.wait_for(self.message_queue.get(), timeout=1.0)
                await self.handle_incoming_message(message)
                if hasattr(self.message_queue, 'task_done'):
                    self.message_queue.task_done()
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                logger.error(f"Agent {self.agent_id}: Erreur dans la boucle run() en traitant les messages: {e}",
                             exc_info=True)
                await asyncio.sleep(5)

            if self.status == MachineStatusEnum.WORKING:  # L'état WORKING est le prérequis pour tenter de travailler
                await self.simulate_work_cycle()  # simulate_work_cycle vérifiera en interne si l'ordre est ACTIVE_WORKING

            await asyncio.sleep(0.2)

        logger.info(f"Agent {self.agent_id} arrêté.")

    def stop(self):
        self._keep_running = False
        logger.info(f"Demande d'arrêt pour l'agent {self.agent_id}.")
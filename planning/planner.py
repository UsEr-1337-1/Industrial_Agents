import asyncio
import logging
from typing import Optional, List, Set, Dict
from sqlalchemy.orm import Session
from sqlalchemy import desc

from agents.communication import Message, InMemoryCommunicationChannel, MSG_TYPE_STATUS_UPDATE, \
    MSG_TYPE_PLANNING_INSTRUCTION
from db.models import ProductionOrder as ProductionOrderModel, OrderStatusEnum, Machine as MachineModel, \
    MachineStatusEnum
from shared.utils import get_db_session

logger = logging.getLogger(__name__)


class ProductionPlanner:
    def __init__(self, agent_id: str, communication_channel: InMemoryCommunicationChannel, db_session_factory):
        self.agent_id = agent_id
        self.communication_channel = communication_channel
        self.db_session_factory = db_session_factory
        self._keep_running = True
        self.message_queue: Optional[asyncio.Queue[Message]] = None
        self.planning_cycle_interval = 10  # secondes
        self.known_and_ready_machine_agents: Set[str] = set()
        self.machines_pending_confirmation: Set[
            str] = set()  # NOUVEAU: Machines en attente de confirmation de démarrage

        logger.info(f"Planificateur de Production {self.agent_id} initialisé.")

    async def register_with_comm_channel(self):
        self.message_queue = await self.communication_channel.register_agent(self.agent_id)
        await self.communication_channel.subscribe_to_topic("machine_status_updates", self.message_queue)
        logger.info(f"Planificateur {self.agent_id} enregistré et abonné à 'machine_status_updates'.")

    def _get_db_session(self) -> Session:
        gen = self.db_session_factory()
        return next(gen)

    async def handle_incoming_message(self, message: Message):
        logger.debug(f"Planificateur {self.agent_id} a reçu message: {message}")
        if message.message_type == MSG_TYPE_STATUS_UPDATE:
            machine_agent_id = message.sender_id
            machine_db_id = message.payload.get("machine_db_id")
            machine_status_str = message.payload.get("status")
            details = message.payload.get("details")

            if machine_agent_id not in self.known_and_ready_machine_agents:
                self.known_and_ready_machine_agents.add(machine_agent_id)
                logger.info(
                    f"Planificateur: Agent Machine {machine_agent_id} (DB ID {machine_db_id}) est maintenant connu et prêt.")

            logger.info(
                f"Planificateur: Reçu statut de {machine_agent_id} (DB ID {machine_db_id}): {machine_status_str}. Détails: {details}")

            # Si la machine confirme qu'elle est WORKING ou ERROR, elle n'est plus en attente de confirmation
            if machine_status_str in [MachineStatusEnum.WORKING.value, MachineStatusEnum.ERROR.value]:
                if machine_agent_id in self.machines_pending_confirmation:
                    self.machines_pending_confirmation.remove(machine_agent_id)
                    logger.info(
                        f"Planificateur: Agent {machine_agent_id} a confirmé son statut ({machine_status_str}), retiré de pending_confirmation.")

            # Si une machine redevient IDLE (après travail ou au démarrage)
            # elle n'est plus "pending" et cela peut déclencher un nouveau cycle.
            if machine_status_str == MachineStatusEnum.IDLE.value:
                if machine_agent_id in self.machines_pending_confirmation:  # Au cas où elle deviendrait IDLE avant WORKING (improbable)
                    self.machines_pending_confirmation.remove(machine_agent_id)

                # Déclencher un cycle si une machine devient IDLE et que ce n'est pas juste le statut "Agent démarré"
                # d'un agent déjà connu (pour éviter des cycles inutiles si rien n'a changé)
                # ou si c'est une notification de tâche terminée.
                if "Tâche terminée" in (details or "") or "Agent démarré" in (details or ""):
                    logger.info(f"Machine {machine_agent_id} est IDLE. Déclenchement d'un cycle de planification.")
                    await self.run_planning_cycle()

    async def run_planning_cycle(self):
        logger.info(f"Planificateur {self.agent_id}: Début du cycle de planification.")
        db = self._get_db_session()
        try:
            pending_orders: List[ProductionOrderModel] = (
                db.query(ProductionOrderModel)
                .filter(ProductionOrderModel.status == OrderStatusEnum.PENDING)
                .order_by(desc(ProductionOrderModel.priority), ProductionOrderModel.created_at)
                .all()
            )

            if not pending_orders:
                logger.info("Aucun ordre de production en attente (statut PENDING).")
                return

            logger.info(f"{len(pending_orders)} ordre(s) PENDING trouvés. Tentative d'assignation...")

            idle_machines_from_db: List[MachineModel] = (
                db.query(MachineModel)
                .filter(MachineModel.status == MachineStatusEnum.IDLE)
                .all()
            )

            # Machines prêtes, IDLE en DB, ET pas en attente de confirmation par le planificateur
            available_for_assignment: Dict[str, MachineModel] = {}
            for m_db in idle_machines_from_db:
                prospective_agent_id = f"machine_agent_{m_db.id}"
                if prospective_agent_id in self.known_and_ready_machine_agents and \
                        prospective_agent_id not in self.machines_pending_confirmation:
                    available_for_assignment[prospective_agent_id] = m_db

            if not available_for_assignment:
                logger.info("Aucune machine inactive ET prête ET non en attente de confirmation disponible.")
                return

            logger.info(f"{len(available_for_assignment)} machine(s) réellement disponible(s) pour assignation: "
                        f"{ {agent_id: m.name for agent_id, m in available_for_assignment.items()} }.")

            assigned_count = 0
            # Itérer sur une copie pour pouvoir retirer des éléments si on veut assigner plusieurs
            # mais ici on va assigner une machine puis elle sera dans pending_confirmation

            # Utilisons une liste d'agent_id pour éviter de modifier le dict pendant l'itération
            # et pour s'assurer qu'on ne réassigne pas à une machine déjà mise en pending DANS CE CYCLE
            machines_assigned_in_this_specific_cycle_run = set()

            for order_to_assign in pending_orders:
                assigned_this_order = False
                # Chercher une machine disponible qui n'a pas déjà été utilisée dans ce cycle de planification
                for agent_id, machine_model in available_for_assignment.items():
                    if agent_id not in machines_assigned_in_this_specific_cycle_run:
                        logger.info(
                            f"Assignation de l'Ordre ID {order_to_assign.id} ({order_to_assign.description or 'N/A'}) "
                            f"à l'Agent Machine {agent_id} (DB ID {machine_model.id}, Nom: {machine_model.name}).")

                        order_in_session = db.merge(order_to_assign)
                        order_in_session.status = OrderStatusEnum.IN_PROGRESS
                        # La machine mettra à jour son propre current_production_order_id et son statut en DB
                        db.commit()

                        instruction_payload = {
                            "production_order_id": order_in_session.id,
                            "action": "START"
                        }
                        instruction_message = Message(
                            sender_id=self.agent_id,
                            receiver_id=agent_id,
                            message_type=MSG_TYPE_PLANNING_INSTRUCTION,
                            payload=instruction_payload
                        )
                        await self.communication_channel.send_direct_message(instruction_message)

                        self.machines_pending_confirmation.add(agent_id)  # Marquer comme en attente de confirmation
                        machines_assigned_in_this_specific_cycle_run.add(
                            agent_id)  # Marquer comme utilisée dans ce cycle précis
                        assigned_count += 1
                        assigned_this_order = True
                        break  # Sortir de la boucle des machines, passer à l'ordre suivant s'il y en a

                if not assigned_this_order:
                    logger.info(
                        f"Aucune machine disponible n'a pu être trouvée pour l'ordre ID {order_to_assign.id} dans la suite de ce cycle.")
                    # On pourrait arrêter ici si les ordres sont strictement priorisés et qu'on ne peut pas assigner le plus prioritaire
                    # break

            if assigned_count > 0:
                logger.info(f"{assigned_count} ordre(s) assigné(s) aux machines.")
            else:
                logger.info("Aucun nouvel ordre PENDING n'a pu être assigné.")

        except Exception as e:
            logger.error(f"Planificateur {self.agent_id}: Erreur pendant le cycle de planification: {e}", exc_info=True)
            if db.is_active:
                db.rollback()
        finally:
            if db.is_active:
                db.close()
        logger.info(f"Planificateur {self.agent_id}: Fin du cycle de planification.")

    async def run(self):
        await self.register_with_comm_channel()

        logger.info(
            f"Planificateur {self.agent_id}: En attente initiale (2s) pour la découverte des agents machines...")
        await asyncio.sleep(2)

        await self.run_planning_cycle()

        while self._keep_running:
            try:
                message = await asyncio.wait_for(self.message_queue.get(), timeout=self.planning_cycle_interval)
                await self.handle_incoming_message(message)
            except asyncio.TimeoutError:
                logger.debug(
                    f"Planificateur {self.agent_id}: Timeout atteint ({self.planning_cycle_interval}s), lancement du cycle de planification régulier.")
                await self.run_planning_cycle()
            except Exception as e:
                logger.error(f"Planificateur {self.agent_id}: Erreur dans la boucle run(): {e}", exc_info=True)
                await asyncio.sleep(5)

        logger.info(f"Planificateur {self.agent_id} arrêté.")

    def stop(self):
        self._keep_running = False
        logger.info(f"Demande d'arrêt pour le planificateur {self.agent_id}.")
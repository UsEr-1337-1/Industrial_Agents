from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, ForeignKey, Enum as SQLAlchemyEnum, \
    Table
from sqlalchemy.orm import relationship, declarative_base, Mapped, mapped_column
from sqlalchemy.sql import func  # Pour default=func.now()
import enum
from typing import List, Optional
from datetime import datetime

Base = declarative_base()


# Enum pour les statuts
class MachineStatusEnum(enum.Enum):
    IDLE = "idle"
    WORKING = "working"
    ERROR = "error"
    MAINTENANCE = "maintenance"


class OrderStatusEnum(enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    ACTIVE_WORKING = "active_working"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


# Définir Part et Machine AVANT ProductionOrder car ProductionOrder y fait référence directement.
# OrderPartLink peut aussi être défini tôt car il utilise des strings pour ses relations.

class Part(Base):  # Défini tôt car utilisé par OrderPartLink (via string) et ResourceUsageLog
    __tablename__ = "parts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    current_stock: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=10)

    # Relations seront ajoutées après la définition de OrderPartLink et ResourceUsageLog si nécessaire,
    # ou via back_populates, ce qui est mieux.
    order_links: Mapped[List["OrderPartLink"]] = relationship(back_populates="part")
    resource_usages: Mapped[List["ResourceUsageLog"]] = relationship(back_populates="part")

    def __repr__(self):
        return f"<Part(id={self.id}, name='{self.name}', stock={self.current_stock})>"


# Machine doit être défini avant ProductionOrder à cause de foreign_keys=[Machine.current_production_order_id]
class Machine(Base):
    __tablename__ = "machines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    status: Mapped[MachineStatusEnum] = mapped_column(
        SQLAlchemyEnum(MachineStatusEnum, name="machine_status_enum_type"), default=MachineStatusEnum.IDLE)
    current_production_order_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("production_orders.id"),
                                                                       nullable=True)  # Référence à la table "production_orders"

    # Relation pour accéder à l'objet ProductionOrder directement si assigné
    # "ProductionOrder" est en string car ProductionOrder est défini après Machine ou pourrait l'être.
    current_production_order: Mapped[Optional["ProductionOrder"]] = relationship(
        back_populates="assigned_machine_instance",
        foreign_keys=[current_production_order_id]
    )

    maintenance_logs: Mapped[List["MaintenanceLog"]] = relationship(back_populates="machine")
    resource_usages: Mapped[List["ResourceUsageLog"]] = relationship(back_populates="machine")

    def __repr__(self):
        return f"<Machine(id={self.id}, name='{self.name}', status='{self.status.value}')>"


# ProductionOrder est défini après Machine car il utilise Machine.current_production_order_id
class ProductionOrder(Base):
    __tablename__ = "production_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    due_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[OrderStatusEnum] = mapped_column(SQLAlchemyEnum(OrderStatusEnum, name="order_status_enum_type"),
                                                    default=OrderStatusEnum.PENDING)
    priority: Mapped[int] = mapped_column(Integer, default=0)

    part_links: Mapped[List["OrderPartLink"]] = relationship(back_populates="order", cascade="all, delete-orphan")

    # Relation vers la machine qui est assignée à cet ordre.
    # "Machine" est en string pour le type hint, mais Machine.current_production_order_id est une référence directe.
    assigned_machine_instance: Mapped[Optional["Machine"]] = relationship(
        back_populates="current_production_order",
        foreign_keys=[Machine.current_production_order_id]  # Machine doit être défini avant cette ligne
    )

    def __repr__(self):
        return f"<ProductionOrder(id={self.id}, status='{self.status.value}', priority={self.priority})>"


# OrderPartLink peut maintenant être complètement défini car Part et ProductionOrder existent
class OrderPartLink(Base):
    __tablename__ = "order_part_links"
    production_order_id: Mapped[int] = mapped_column(ForeignKey("production_orders.id"), primary_key=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("parts.id"), primary_key=True)
    quantity_required: Mapped[int] = mapped_column(Integer, nullable=False)

    order: Mapped["ProductionOrder"] = relationship(
        back_populates="part_links")  # ProductionOrder est maintenant défini
    part: Mapped["Part"] = relationship(back_populates="order_links")  # Part est défini

    def __repr__(self):
        return f"<OrderPartLink order_id={self.production_order_id} part_id={self.part_id} qty={self.quantity_required}>"


class MaintenanceLog(Base):
    __tablename__ = "maintenance_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    machine_id: Mapped[int] = mapped_column(Integer, ForeignKey(
        "machines.id"))  # "machines.id" est une référence de table string
    machine: Mapped["Machine"] = relationship(
        back_populates="maintenance_logs")  # "Machine" est ok car Machine est défini
    start_time: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    description: Mapped[str] = mapped_column(String)
    log_type: Mapped[str] = mapped_column(String, default="corrective")

    def __repr__(self):
        return f"<MaintenanceLog(id={self.id}, machine_id={self.machine_id}, type='{self.log_type}')>"


class ResourceUsageLog(Base):
    __tablename__ = "resource_usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    machine_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("machines.id"), nullable=True)
    machine: Mapped[Optional["Machine"]] = relationship(back_populates="resource_usages")  # "Machine" est ok

    part_id: Mapped[int] = mapped_column(Integer,
                                         ForeignKey("parts.id"))  # "parts.id" est une référence de table string
    part: Mapped["Part"] = relationship(back_populates="resource_usages")  # "Part" est ok car Part est défini

    production_order_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("production_orders.id"),
                                                               nullable=True)
    quantity_used: Mapped[int] = mapped_column(Integer, default=1)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    def __repr__(self):
        return f"<ResourceUsageLog(id={self.id}, part_id={self.part_id}, quantity={self.quantity_used})>"
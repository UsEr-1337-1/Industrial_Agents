from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from db.models import Base, Part, Machine, ProductionOrder, OrderPartLink, \
    OrderStatusEnum  # Importez vos modèles et Enums
from shared.config import DATABASE_URL  # Importez votre config
import logging

logger = logging.getLogger(__name__)

engine = create_engine(DATABASE_URL, echo=False)  # Mettez echo=True pour voir les SQL en dev si besoin
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db_session() -> Session:
    """Crée et retourne une nouvelle session de base de données."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_db_and_tables():
    """Crée toutes les tables dans la base de données définies par les modèles SQLAlchemy."""
    logger.info(f"Tentative de création des tables pour la base de données sur {DATABASE_URL}...")
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Tables créées avec succès (ou existent déjà).")
    except Exception as e:
        logger.error(f"Erreur lors de la création des tables : {e}")
        raise


def initialize_sample_data(db: Session):
    """Initialise la base de données avec quelques données de test si elle est vide."""
    logger.info("Vérification et initialisation des données de test...")

    if db.query(Part).count() == 0:
        logger.info("Aucune pièce trouvée, ajout de données de test pour les pièces...")
        parts_data = [
            {"name": "Moteur V6 TDI", "description": "Moteur diesel puissant", "current_stock": 50,
             "low_stock_threshold": 10},
            {"name": "Roue Aluminium 18\"", "description": "Jante en alliage léger", "current_stock": 200,
             "low_stock_threshold": 40},
            {"name": "Vis de culasse M10", "description": "Vis haute résistance", "current_stock": 1000,
             "low_stock_threshold": 100},
            {"name": "Portière Avant Gauche - Rouge", "description": "PAN Rouge", "current_stock": 30,
             "low_stock_threshold": 5},
            {"name": "Boîte de vitesses DSG7", "description": "Transmission automatique", "current_stock": 25,
             "low_stock_threshold": 5},
        ]
        for p_data in parts_data:
            db.add(Part(**p_data))
        try:
            db.commit()
            logger.info(f"{len(parts_data)} pièces ajoutées.")
        except Exception as e:
            db.rollback()
            logger.error(f"Erreur lors de l'ajout des pièces : {e}")
    else:
        logger.info("Des pièces existent déjà, pas d'ajout.")

    if db.query(Machine).count() == 0:
        logger.info("Aucune machine trouvée, ajout de données de test pour les machines...")
        machines_data = [
            {"name": "Presse Hydraulique Alpha"},
            {"name": "Robot d'Assemblage KUKA-1"},
            {"name": "Poste de Peinture Automatisé Delta"},
        ]
        for m_data in machines_data:
            db.add(Machine(**m_data))
        try:
            db.commit()
            logger.info(f"{len(machines_data)} machines ajoutées.")
        except Exception as e:
            db.rollback()
            logger.error(f"Erreur lors de l'ajout des machines : {e}")
    else:
        logger.info("Des machines existent déjà, pas d'ajout.")

    if db.query(ProductionOrder).count() == 0:
        logger.info("Aucun ordre de production trouvé, ajout de données de test...")
        part_moteur = db.query(Part).filter(Part.name == "Moteur V6 TDI").first()
        part_roue = db.query(Part).filter(Part.name == 'Roue Aluminium 18"').first()
        part_boite = db.query(Part).filter(Part.name == "Boîte de vitesses DSG7").first()

        if part_moteur and part_roue and part_boite:
            orders_definitions = [
                {
                    "description": "Assemblage SUV Model X Premium",
                    "priority": 1,
                    "links_data": [
                        {"part": part_moteur, "quantity": 1},
                        {"part": part_roue, "quantity": 4},
                        {"part": part_boite, "quantity": 1}
                    ]
                },
                {
                    "description": "Assemblage Berline Model S Standard",
                    "priority": 0,
                    "links_data": [
                        {"part": part_moteur, "quantity": 1},
                        {"part": part_roue, "quantity": 4}  # Pas de boite DSG7 pour la standard
                    ]
                },
                {
                    "description": "Assemblage SUV Model Y Performance",
                    "priority": 2,  # Plus prioritaire
                    "links_data": [
                        {"part": part_moteur, "quantity": 1},
                        {"part": part_roue, "quantity": 4},
                        {"part": part_boite, "quantity": 1}
                    ]
                },
            ]

            created_orders_count = 0
            for o_def in orders_definitions:
                new_order = ProductionOrder(
                    description=o_def["description"],
                    priority=o_def["priority"],
                    status=OrderStatusEnum.PENDING
                )
                db.add(new_order)  # Ajouter l'ordre pour qu'il ait un ID avant de créer les liens si nécessaire
                # ou s'appuyer sur la session pour flusher au commit.
                # Avec back_populates, la session gère bien les liens.

                for link_data in o_def["links_data"]:
                    order_part_link = OrderPartLink(
                        order=new_order,  # Lie à l'objet SQLAlchemy ProductionOrder
                        part=link_data["part"],  # Lie à l'objet SQLAlchemy Part
                        quantity_required=link_data["quantity"]
                    )
                    db.add(order_part_link)
                created_orders_count += 1

            try:
                db.commit()
                logger.info(f"{created_orders_count} ordres de production ajoutés avec liens de pièces détaillés.")
            except Exception as e:
                db.rollback()
                logger.error(f"Erreur lors de l'ajout des ordres de production : {e}")
        else:
            required_parts_missing = [p for p, n in
                                      [(part_moteur, "Moteur"), (part_roue, "Roue"), (part_boite, "Boite")] if not p]
            logger.warning(
                f"Pièces de test ({', '.join(n for n in required_parts_missing)}) non trouvées, impossible de créer les ordres de production de test.")
    else:
        logger.info("Des ordres de production existent déjà, pas d'ajout.")

    logger.info("Initialisation des données de test terminée.")


if __name__ == '__main__':
    print("Script utilitaire de base de données.")
    # Décommentez les lignes suivantes pour une initialisation manuelle de la DB.
    # Attention: cela va essayer de créer les tables et ajouter les données de test.
    # print("Création des tables...")
    # create_db_and_tables()
    # print("Initialisation des données de test...")
    # db_session_gen = get_db_session()
    # db = next(db_session_gen)
    # try:
    #    initialize_sample_data(db)
    # finally:
    #    db.close()
    # print("Opérations manuelles terminées.")
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy import func  # Pour func.sum et func.count

from db.models import ProductionOrder, OrderStatusEnum, ResourceUsageLog, Part
from shared.utils import get_db_session  # Pour obtenir une session si on le lance en standalone

logger = logging.getLogger(__name__)


def generate_production_summary(db: Session, start_date: datetime, end_date: datetime) -> dict:
    """
    Génère un résumé de la production pour une période donnée.
    KPIs :
    - Nombre total d'ordres complétés.
    - Consommation totale de chaque pièce.
    """
    report = {
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "orders_completed_count": 0,
        "parts_consumed": [],  # Liste de dicts: {"part_name": str, "part_id": int, "total_consumed": int}
        "error": None
    }

    try:
        # S'assurer que les dates sont timezone-aware (UTC) si la DB stocke des timestamps UTC
        # Si vos dates sont naïves, vous pourriez avoir besoin de les localiser ou de les convertir.
        # Pour SQLite, les comparaisons de dates naïves fonctionnent généralement si tout est naïf.
        # Si completed_at est stocké en UTC (comme recommandé), start_date et end_date devraient l'être aussi.
        # Exemple : if start_date.tzinfo is None: start_date = start_date.replace(tzinfo=timezone.utc)

        # 1. Nombre d'ordres complétés
        completed_orders_query = db.query(ProductionOrder) \
            .filter(
            ProductionOrder.status == OrderStatusEnum.COMPLETED,
            ProductionOrder.completed_at >= start_date,
            ProductionOrder.completed_at <= end_date
        )
        report["orders_completed_count"] = completed_orders_query.count()
        logger.info(f"Rapport: {report['orders_completed_count']} ordres complétés entre {start_date} et {end_date}.")

        # 2. Consommation totale de chaque pièce
        parts_consumed_query = db.query(
            Part.id,
            Part.name,
            func.sum(ResourceUsageLog.quantity_used).label("total_consumed")
        ) \
            .join(ResourceUsageLog, Part.id == ResourceUsageLog.part_id) \
            .filter(
            ResourceUsageLog.timestamp >= start_date,
            ResourceUsageLog.timestamp <= end_date
        ) \
            .group_by(Part.id, Part.name) \
            .order_by(Part.name) \
            .all()

        for part_id, part_name, total_consumed in parts_consumed_query:
            report["parts_consumed"].append({
                "part_id": part_id,
                "part_name": part_name,
                "total_consumed": total_consumed or 0  # Mettre 0 si total_consumed est None
            })
        logger.info(f"Rapport: {len(report['parts_consumed'])} types de pièces consommées sur la période.")

    except Exception as e:
        logger.error(f"Erreur lors de la génération du rapport de production: {e}", exc_info=True)
        report["error"] = str(e)

    return report


# Pour tester ce module directement (optionnel)
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')

    # Définir une période de test (par exemple, les dernières 24 heures)
    # Assurez-vous que vos timestamps dans la DB sont cohérents (UTC ou naïfs)
    # Si vos timestamps DB sont UTC, utilisez des datetimes UTC ici.
    # Si naïfs, utilisez des datetimes naïfs. SQLite stocke souvent des strings naïves.

    # Exemple avec dates naïves (si votre DB SQLite utilise des datetimes naïfs par défaut via func.now())
    # end_test_date = datetime.now()
    # start_test_date = end_test_date - timedelta(days=1)

    # Exemple avec dates UTC (si vous stockez en UTC)
    end_test_date_utc = datetime.now(timezone.utc)
    start_test_date_utc = end_test_date_utc - timedelta(days=1)

    logger.info(f"Génération d'un rapport de test pour la période: {start_test_date_utc} à {end_test_date_utc}")

    db_session_gen = get_db_session()
    db = next(db_session_gen)
    try:
        # Pour que ce test fonctionne, il faut que des données aient été générées par main.py
        # et que des ResourceUsageLog existent.
        # Vous pourriez avoir besoin d'exécuter main.py pour peupler la DB d'abord.
        test_report = generate_production_summary(db, start_test_date_utc, end_test_date_utc)

        if test_report.get("error"):
            logger.error(f"Erreur dans le rapport de test: {test_report['error']}")
        else:
            logger.info("Rapport de Test Généré:")
            logger.info(f"  Période: {test_report['period_start']} - {test_report['period_end']}")
            logger.info(f"  Ordres Complétés: {test_report['orders_completed_count']}")
            logger.info("  Pièces Consommées:")
            if test_report['parts_consumed']:
                for part_info in test_report['parts_consumed']:
                    logger.info(
                        f"    - {part_info['part_name']} (ID: {part_info['part_id']}): {part_info['total_consumed']}")
            else:
                logger.info("    Aucune pièce consommée sur la période.")
    finally:
        db.close()
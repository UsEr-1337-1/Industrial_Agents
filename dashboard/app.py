# dashboard/app.py (Fichier Complet)

import logging
from flask import Flask, render_template, jsonify, request  # request ajouté
from sqlalchemy.orm import Session
from werkzeug.serving import make_server
import threading
from datetime import datetime, timezone  # datetime, timezone ajoutés

from shared.utils import get_db_session
from db.models import Machine as MachineModel, Part as PartModel, ProductionOrder as ProductionOrderModel, \
    OrderStatusEnum, MachineStatusEnum
from reporting.generate_report import generate_production_summary  # <-- Importer la fonction de rapport

logger = logging.getLogger(__name__)
app = Flask(__name__)


def get_data_for_dashboard():
    db_session_generator = get_db_session()
    db: Session = next(db_session_generator)
    data = {"machines": [], "parts": [], "production_orders": []}
    try:
        machines = db.query(MachineModel).order_by(MachineModel.id).all()
        for m in machines:
            data["machines"].append({
                "id": m.id, "name": m.name,
                "status": m.status.value if m.status else 'N/A',
                "current_order_id": m.current_production_order_id
            })
        parts = db.query(PartModel).order_by(PartModel.name).all()
        for p in parts:
            data["parts"].append({
                "id": p.id, "name": p.name, "current_stock": p.current_stock,
                "low_stock_threshold": p.low_stock_threshold,
                "is_low_stock": p.current_stock < p.low_stock_threshold
            })

        active_orders = db.query(ProductionOrderModel) \
            .filter(ProductionOrderModel.status.notin_([OrderStatusEnum.COMPLETED, OrderStatusEnum.CANCELLED])) \
            .order_by(ProductionOrderModel.priority.desc(), ProductionOrderModel.created_at.desc()) \
            .limit(20).all()

        recent_completed_orders = db.query(ProductionOrderModel) \
            .filter(ProductionOrderModel.status == OrderStatusEnum.COMPLETED) \
            .order_by(ProductionOrderModel.completed_at.desc()) \
            .limit(5).all()

        all_orders_to_display = active_orders + recent_completed_orders

        for po in all_orders_to_display:
            order_data = {
                "id": po.id, "description": po.description or f"Ordre #{po.id}",
                "status": po.status.value if po.status else 'N/A',
                "priority": po.priority,
                "created_at": po.created_at.strftime("%Y-%m-%d %H:%M:%S") if po.created_at else 'N/A',
                "completed_at": po.completed_at.strftime("%Y-%m-%d %H:%M:%S") if po.completed_at else 'N/A',
                "assigned_machine_id": None, "assigned_machine_name": None
            }
            # Trouver la machine assignée (si l'ordre n'est pas terminé/annulé)
            if po.status not in [OrderStatusEnum.COMPLETED, OrderStatusEnum.CANCELLED]:
                # Chercher dans les machines si l'une d'elles a cet ID d'ordre
                assigned_machine = db.query(MachineModel).filter(
                    MachineModel.current_production_order_id == po.id).first()
                if assigned_machine:
                    order_data["assigned_machine_id"] = assigned_machine.id
                    order_data["assigned_machine_name"] = assigned_machine.name
            data["production_orders"].append(order_data)

    except Exception as e:
        logger.error(f"Erreur lors de la récupération des données pour le dashboard: {e}", exc_info=True)
    finally:
        db.close()
    return data


@app.route('/')
def dashboard_page():
    return render_template('dashboard.html', title="Tableau de Bord Usine")


@app.route('/api/system_status')
def api_system_status():
    try:
        data = get_data_for_dashboard()
        return jsonify(data)
    except Exception as e:
        logger.error(f"Erreur API /api/system_status: {e}", exc_info=True)
        return jsonify({"error": "Erreur interne du serveur"}), 500


# NOUVELLE ROUTE API POUR LES RAPPORTS
@app.route('/api/reports/production_summary')
def api_production_summary_report():
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')

    if not start_date_str or not end_date_str:
        return jsonify({"error": "Les paramètres 'start_date' et 'end_date' (YYYY-MM-DD) sont requis."}), 400

    try:
        # Convertir les strings en objets datetime.
        # Pour la DB, on utilisera des objets datetime naïfs si la DB stocke comme ça (cas de SQLite par défaut)
        # Ou des objets timezone-aware (UTC) si la DB stocke en UTC.
        # `datetime.fromisoformat` gère les strings YYYY-MM-DD.
        # On ajoute minuit pour la date de début et fin de journée pour la date de fin.
        start_date = datetime.fromisoformat(start_date_str)
        end_date = datetime.fromisoformat(end_date_str).replace(hour=23, minute=59, second=59, microsecond=999999)

        # Si vos timestamps en DB sont UTC (comme `datetime.now(timezone.utc)` le suggère)
        # il faut rendre ces dates "aware" ou s'assurer que la comparaison est correcte.
        # Pour l'instant, on suppose une comparaison directe possible avec les dates naïves de la DB (si SQLite)
        # ou que la DB gère la conversion si les types sont corrects.
        # Si vous utilisez completed_at = datetime.now(timezone.utc), alors :
        # start_date = datetime.fromisoformat(start_date_str).replace(tzinfo=timezone.utc)
        # end_date = datetime.fromisoformat(end_date_str).replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc)


    except ValueError:
        return jsonify({"error": "Format de date invalide. Utilisez YYYY-MM-DD."}), 400

    db_session_generator = get_db_session()
    db: Session = next(db_session_generator)
    try:
        report_data = generate_production_summary(db, start_date, end_date)
        if report_data.get("error"):
            return jsonify({"error": f"Erreur lors de la génération du rapport: {report_data['error']}"}), 500
        return jsonify(report_data)
    except Exception as e:
        logger.error(f"Erreur API /api/reports/production_summary: {e}", exc_info=True)
        return jsonify({"error": "Erreur interne du serveur lors de la génération du rapport"}), 500
    finally:
        db.close()


class FlaskServerThread(threading.Thread):
    def __init__(self, flask_app, host='0.0.0.0', port=5000):
        super().__init__()
        self.flask_app = flask_app
        self.host = host
        self.port = port
        self.srv = make_server(self.host, self.port, self.flask_app, threaded=True)
        self.ctx = self.flask_app.app_context()
        self.ctx.push()
        self.daemon = True

    def run(self):
        logger.info(f"Démarrage du serveur Flask sur http://{self.host}:{self.port}")
        self.srv.serve_forever()

    def shutdown(self):
        logger.info("Arrêt du serveur Flask...")
        self.srv.shutdown()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
    # ... (code de test si besoin) ...
    app.run(debug=True, host='0.0.0.0', port=5000)
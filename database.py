import os
import logging
import json # Necesario para serializar/deserializar JSONB
from datetime import datetime

from supabase import create_client, Client
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Asegúrate de que las variables de entorno están configuradas
if not SUPABASE_URL or not SUPABASE_KEY:
    logging.error("Las variables de entorno SUPABASE_URL o SUPABASE_KEY no están configuradas.")
    raise ValueError("Configuración de Supabase incompleta. Por favor, configura tu .env o variables de entorno.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Funciones para la tabla 'users' ---
def get_user(user_id: int):
    """Obtiene datos de un usuario por su ID de Telegram."""
    try:
        response = supabase.table("users2").select("*").eq("user_id", user_id).execute()
        data = response.data
        return data[0] if data else None
    except Exception as e:
        logging.error(f"Error al obtener usuario {user_id}: {e}")
        return None

def add_user(user_id: int, referred_by=None, initial_points=0):
    """Añade un nuevo usuario a la base de datos con puntos iniciales y prioridad por defecto."""
    user = get_user(user_id)
    if user:
        logging.warning(f"Usuario {user_id} ya existe. Saltando adición.")
        return False

    # created_at ya no es necesario aquí si la columna en DB tiene DEFAULT NOW()
    data = {
        "user_id": user_id,
        "points": initial_points,
        "referred_by": referred_by,
        "priority_level": 2 # Prioridad por defecto: 2 (Normal/Baja), debe coincidir con el DEFAULT en SQL
    }
    try:
        response = supabase.table("users2").insert(data).execute()
        if response.data:
            logging.info(f"Usuario {user_id} añadido a la BD. Puntos: {initial_points}, Prioridad: 2.")
            return True
        logging.warning(f"No se pudo añadir usuario {user_id}: {response.json()}.")
        return False
    except Exception as e:
        logging.warning(f"Error al añadir usuario {user_id} (puede que ya exista): {e}.")
        return False

def update_user_points(user_id: int, amount: int):
    """Actualiza los puntos de un usuario."""
    user = get_user(user_id)
    if not user:
        logging.warning(f"Usuario {user_id} no encontrado para actualizar puntos.")
        return None

    new_points = user["points"] + amount
    try:
        response = supabase.table("users2").update({"points": new_points}).eq("user_id", user_id).execute()
        if response.data:
            logging.info(f"Puntos de usuario {user_id} actualizados en {amount} (total: {new_points}).")
            return response.data[0] # Retorna el usuario actualizado
        logging.error(f"Error al actualizar puntos para el usuario {user_id}: {response.json()}.")
        return None
    except Exception as e:
        logging.error(f"Error al actualizar puntos para el usuario {user_id}: {e}.")
        return None

def get_user_points(user_id: int) -> int:
    """Obtiene el saldo actual de puntos de un usuario."""
    user = get_user(user_id)
    return user["points"] if user else 0

def get_user_priority(user_id: int) -> int:
    """Obtiene el nivel de prioridad actual de un usuario."""
    user = get_user(user_id)
    # Asume que 'priority_level' existe si el usuario existe, si no, usa el default 2
    return user.get("priority_level", 2) if user else 2

def update_user_priority(user_id: int, new_priority_level: int):
    """
    Actualiza el nivel de prioridad de un usuario si el 'new_priority_level' es "mejor" (numéricamente menor)
    que el actual.
    """
    user = get_user(user_id)
    if not user:
        logging.warning(f"Usuario {user_id} no encontrado para actualizar prioridad.")
        return False

    current_priority = user.get("priority_level", 2) # Obtener la prioridad actual del objeto user
    
    if new_priority_level < current_priority: # Si la nueva prioridad es MENOR (más alta)
        try:
            response = supabase.table("users2").update({'priority_level': new_priority_level}).eq('user_id', user_id).execute()
            if response.data:
                logging.info(f"Prioridad del usuario {user_id} actualizada de {current_priority} a {new_priority_level}.")
                return True
            logging.error(f"Error al actualizar prioridad del usuario {user_id}: {response.json()}.")
            return False
        except Exception as e:
            logging.error(f"Error al actualizar prioridad del usuario {user_id}: {e}.")
            return False
    else:
        logging.info(f"La nueva prioridad {new_priority_level} no es mejor que la actual {current_priority} para el usuario {user_id}.")
        return False

# --- Funciones para la tabla 'generation_queue' ---
async def add_generation_job(user_id: int, chat_id: int, message_id: int, filepath: str, workflow_content: dict, selected_workflow_name: str, priority_level: int):
    """
    Añade un trabajo de generación a la cola persistente en Supabase.
    Almacena el workflow_content como un string JSON.
    """
    job_data = {
        'user_id': user_id,
        'chat_id': chat_id,
        'message_id': message_id,
        'filepath': filepath,
        'workflow_content': json.dumps(workflow_content), # Se serializa a JSON string para la BD
        'selected_workflow_name': selected_workflow_name,
        'status': 'pending',
        'priority_level': priority_level
    }
    try:
        response = supabase.table("generation_queue2").insert(job_data).execute()
        if response.data:
            logging.info(f"Trabajo de generación para {user_id} añadido a la cola persistente con prioridad {priority_level}. ID: {response.data[0]['id']}.")
            return response.data[0]['id'] # Retorna el ID UUID del trabajo insertado
        logging.error(f"Error al añadir trabajo de generación: {response.json()}.")
        return None
    except Exception as e:
        logging.error(f"Error al añadir trabajo de generación para usuario {user_id}: {e}.")
        return None

async def get_next_generation_job():
    """
    Obtiene el siguiente trabajo de la cola con la prioridad más alta (menor número)
    y el 'created_at' más antiguo, y lo marca como 'processing' de forma atómica.
    """
    try:
        # 1. Seleccionar el trabajo más prioritario y más antiguo que esté 'pending'
        # Usamos .rpc('get_next_job_and_mark_processing') si tuvieras una función RPC para esto,
        # pero para el estilo actual, lo haremos en dos pasos.
        response = supabase.table("generation_queue2") \
            .select('*') \
            .eq('status', 'pending') \
            .order('priority_level', asc=True) \
            .order('created_at', asc=True) \
            .limit(1) \
            .execute()

        if not response.data:
            return None # No hay trabajos pendientes en la cola

        job = response.data[0]
        job_id = job['id']

        # 2. Intentar actualizar el estado a 'processing' de forma transaccional/atómica
        # Se usa 'eq('status', 'pending')' para asegurar que solo se actualice si el estado aún es 'pending'.
        update_response = supabase.table("generation_queue2") \
            .update({'status': 'processing', 'started_at': datetime.now().isoformat()}) \
            .eq('id', job_id) \
            .eq('status', 'pending') \
            .execute()

        if update_response.data:
            logging.info(f"Trabajo {job_id} marcado como 'processing'.")
            # Deserializar 'workflow_content' de JSON string a dict antes de retornar
            job['workflow_content'] = json.loads(job['workflow_content'])
            return job
        else:
            # Si no se pudo actualizar (ej. otro worker lo tomó justo antes), se loguea y se devuelve None.
            logging.warning(f"Trabajo {job_id} ya fue tomado o su estado cambió. Reintentando la búsqueda...")
            return None

    except Exception as e:
        logging.error(f"Error al obtener o marcar trabajo de generación en cola: {e}.")
        return None

async def update_generation_job_status(job_id: str, status: str, output_files_urls: list = None, error_message: str = None):
    """
    Actualiza el estado de un trabajo de generación en la cola persistente.
    Permite establecer 'completed', 'failed', 'refunded', 'canceled'.
    """
    update_data = {'status': status}
    if status == 'completed':
        update_data['completed_at'] = datetime.now().isoformat()
        if output_files_urls:
            update_data['output_files_urls'] = json.dumps(output_files_urls) # Serializa la lista de URLs a JSON string
    elif status in ('failed', 'refunded', 'canceled'): # Estados que indican que el trabajo ya no está activo/pendiente
        update_data['error_message'] = error_message
        update_data['completed_at'] = datetime.now().isoformat() # Marcar como completado (con fallo/reembolso/cancelado)


    try:
        response = supabase.table("generation_queue2").update(update_data).eq('id', job_id).execute()
        if response.data:
            logging.info(f"Estado del trabajo {job_id} actualizado a {status}.")
        else:
            logging.error(f"Error al actualizar estado del trabajo {job_id}: {response.json()}.")
    except Exception as e:
        logging.error(f"Error en update_generation_job_status para {job_id}: {e}.")

async def get_uncompleted_processing_jobs():
    """
    Recupera trabajos que quedaron en estado 'processing' de una sesión anterior
    (ej. por un crash del worker), para que puedan ser marcados como fallidos y se reembolsen.
    """
    try:
        response = supabase.table("generation_queue2") \
            .select('id, user_id, chat_id, filepath, selected_workflow_name') \
            .eq('status', 'processing') \
            .execute()
        
        if response.data:
            logging.warning(f"Encontrados {len(response.data)} trabajos en estado 'processing' no completados tras un reinicio.")
        return response.data
    except Exception as e:
        logging.error(f"Error al recuperar trabajos 'processing' no completados: {e}.")
        return []

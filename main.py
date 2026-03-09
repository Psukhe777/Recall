# ============================================================
# RECALL SaaS — FastAPI Backend (FIXED VERSION)
# Healthcare-agnostic automated SMS recall engine
# Stack: FastAPI + Supabase + Twilio
# ============================================================

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, validator
from typing import Optional, List, Dict, Any
from datetime import datetime, date, timedelta
from enum import Enum
import httpx
import os
import re
import logging
import secrets
from supabase import create_client, Client
from twilio.rest import Client as TwilioClient
from twilio.request_validator import RequestValidator
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("recall")

# ============================================================
# CONFIG & VALIDATION
# ============================================================
required_env_vars = [
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_FROM_NUMBER",
    "API_SECRET_KEY"
]

missing = [var for var in required_env_vars if not os.getenv(var)]
if missing:
    raise RuntimeError(f"❌ Missing required environment variables: {', '.join(missing)}")

SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_SERVICE_KEY"]
TWILIO_SID      = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_TOKEN    = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM     = os.environ["TWILIO_FROM_NUMBER"]
API_SECRET      = os.environ["API_SECRET_KEY"]
ENVIRONMENT     = os.getenv("ENVIRONMENT", "development")

supabase: Client  = create_client(SUPABASE_URL, SUPABASE_KEY)
twilio            = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
twilio_validator  = RequestValidator(TWILIO_TOKEN)

# ============================================================
# APP
# ============================================================
app = FastAPI(
    title="Recall SaaS API",
    description="Healthcare-agnostic automated patient recall via SMS",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# MODELS
# ============================================================
class RecallStatus(str, Enum):
    pending    = "pending"
    in_progress = "in_progress"
    booked     = "booked"
    completed  = "completed"
    opted_out  = "opted_out"
    failed     = "failed"
    snoozed    = "snoozed"

class PatientCreate(BaseModel):
    external_id:    Optional[str]   = None
    first_name:     str
    last_name:      str
    phone:          str
    email:          Optional[str]   = None
    date_of_birth:  Optional[date]  = None
    metadata:       Dict[str, Any]  = {}

    @validator("phone")
    def validate_phone(cls, v):
        if not re.match(r"^\+\d{7,15}$", v):
            raise ValueError("Phone must be E.164 format: +61412345678")
        return v

class RecallCreate(BaseModel):
    patient_id:         str
    template_id:        Optional[str]  = None
    recall_type:        str
    last_appointment:   Optional[date] = None
    due_date:           date
    booking_link:       Optional[str]  = None
    notes:              Optional[str]  = None
    priority:           int = 1

    @validator("due_date")
    def validate_due_date(cls, v):
        if v < date.today():
            raise ValueError("due_date cannot be in the past")
        return v

class BulkRecallImport(BaseModel):
    patients: List[Dict[str, Any]]
    template_id: Optional[str] = None
    recall_interval_days: int = 180

class TenantConfig(BaseModel):
    name:           str
    slug:           str  # FIXED: Now required
    service_type:   str
    phone_number:   Optional[str] = None
    timezone:       str   = "UTC"
    country_code:   str   = "AU"
    twilio_from:    Optional[str] = None
    settings:       Dict[str, Any] = {}

# ============================================================
# AUTH HELPERS (FIXED)
# ============================================================
def verify_api_key(x_api_key: str = Header(...)):
    """Verify global API secret (for admin/cron endpoints only)"""
    if x_api_key != API_SECRET:
        raise HTTPException(status_code=401, detail="Invalid API key")

def verify_tenant_auth(tenant_id: str, authorization: str = Header(...)) -> dict:
    """Verify Bearer token matches tenant's API key"""
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Authorization header must be 'Bearer <api_key>'")
    
    token = authorization.replace("Bearer ", "")
    
    # Get tenant and verify key
    try:
        tenant_res = supabase.table("tenants").select("*").eq("id", tenant_id).single().execute()
        tenant = tenant_res.data
        
        if not tenant:
            raise HTTPException(404, "Tenant not found")
        
        if tenant.get("api_key") != token:
            raise HTTPException(401, "Invalid tenant API key")
        
        if not tenant.get("active", True):
            raise HTTPException(403, "Tenant account is inactive")
        
        return tenant
        
    except Exception as e:
        logger.error(f"Tenant auth error: {e}")
        raise HTTPException(401, "Authentication failed")

def get_tenant(tenant_id: str) -> dict:
    """Get tenant by ID (for internal use)"""
    res = supabase.table("tenants").select("*").eq("id", tenant_id).single().execute()
    if not res.data:
        raise HTTPException(404, "Tenant not found")
    return res.data

# ============================================================
# SMS ENGINE
# ============================================================
def render_template(template: str, patient: dict, tenant: dict, recall: dict) -> str:
    """Replace template variables with actual values"""
    replacements = {
        "{first_name}":      patient.get("first_name", ""),
        "{last_name}":       patient.get("last_name", ""),
        "{full_name}":       f"{patient.get('first_name','')} {patient.get('last_name','')}",
        "{practice_name}":   tenant.get("name", "Your practice"),
        "{practice_phone}":  tenant.get("phone_number", ""),
        "{booking_link}":    recall.get("booking_link", ""),
        "{pet_name}":        patient.get("metadata", {}).get("pet_name", "your pet"),
        "{due_date}":        recall.get("due_date", ""),
    }
    msg = template
    for key, val in replacements.items():
        msg = msg.replace(key, str(val))
    return msg

def send_sms(to: str, body: str, tenant: dict, recall_id: str = None,
             patient_id: str = None, sequence_step: int = 0) -> dict:
    """Send SMS via Twilio and log to DB"""
    from_number = tenant.get("twilio_from") or TWILIO_FROM

    try:
        msg = twilio.messages.create(to=to, from_=from_number, body=body)
        status = "sent"
        twilio_sid = msg.sid
        error_code = None
        error_msg = None
    except Exception as e:
        logger.error(f"SMS send failed to {to}: {e}")
        status = "failed"
        twilio_sid = None
        error_code = "SEND_ERROR"
        error_msg = str(e)

    # Log to DB
    log_entry = {
        "tenant_id":       tenant["id"],
        "patient_id":      patient_id,
        "recall_id":       recall_id,
        "twilio_sid":      twilio_sid,
        "direction":       "outbound",
        "from_number":     from_number,
        "to_number":       to,
        "body":            body,
        "status":          status,
        "error_code":      error_code,
        "error_message":   error_msg,
        "sequence_step":   sequence_step,
        "sent_at":         datetime.utcnow().isoformat() if status == "sent" else None,
    }
    supabase.table("sms_messages").insert(log_entry).execute()
    return {"status": status, "twilio_sid": twilio_sid}

# ============================================================
# INBOUND SMS HANDLER (FIXED)
# ============================================================
def detect_intent(body: str) -> str:
    """Detect user intent from SMS body"""
    body_lower = body.lower().strip()
    
    # STOP intent
    if any(kw in body_lower for kw in ["stop", "cancel", "unsubscribe", "remove"]):
        return "STOP"
    
    # BOOK intent
    if any(kw in body_lower for kw in ["book", "yes", "y", "1", "confirm", "schedule"]):
        return "BOOK"
    
    # SNOOZE intent
    if any(kw in body_lower for kw in ["later", "not now", "snooze", "remind me"]):
        return "SNOOZE"
    
    # START intent (opt back in)
    if any(kw in body_lower for kw in ["start", "unstop", "opt in"]):
        return "START"
    
    return "UNKNOWN"

def handle_inbound_sms(from_num: str, to_num: str, body: str, sid: str) -> str:
    """Handle inbound SMS and update database (FIXED)"""
    try:
        # Find patient by phone
        patient_res = supabase.table("patients").select("*").eq("phone", from_num).execute()
        
        if not patient_res.data:
            logger.warning(f"Inbound SMS from unknown number: {from_num}")
            return "Thanks for your message. We couldn't find your number in our system. Please contact us directly."
        
        patient = patient_res.data[0]
        tenant = get_tenant(patient["tenant_id"])
        
        # Detect intent
        intent = detect_intent(body)
        
        # Find active recall
        recall = None
        recall_res = supabase.table("recalls").select("*").eq("patient_id", patient["id"]).in_(
            "status", ["pending", "in_progress", "snoozed"]
        ).order("created_at", desc=True).limit(1).execute()
        
        if recall_res.data:
            recall = recall_res.data[0]
        
        # Log inbound message
        supabase.table("inbound_responses").insert({
            "tenant_id": patient["tenant_id"],
            "patient_id": patient["id"],
            "recall_id": recall["id"] if recall else None,
            "from_number": from_num,
            "to_number": to_num,
            "body": body,
            "intent": intent,
            "twilio_sid": sid,
            "handled": True
        }).execute()
        
        # Log SMS message
        supabase.table("sms_messages").insert({
            "tenant_id": patient["tenant_id"],
            "patient_id": patient["id"],
            "recall_id": recall["id"] if recall else None,
            "twilio_sid": sid,
            "direction": "inbound",
            "from_number": from_num,
            "to_number": to_num,
            "body": body,
            "status": "received"
        }).execute()
        
        # Handle intent
        if intent == "STOP":
            # Opt out patient
            supabase.table("patients").update({
                "opted_out": True,
                "opted_out_at": datetime.utcnow().isoformat()
            }).eq("id", patient["id"]).execute()
            
            # Mark all active recalls as opted out
            supabase.table("recalls").update({
                "status": "opted_out"
            }).eq("patient_id", patient["id"]).in_(
                "status", ["pending", "in_progress", "snoozed"]
            ).execute()
            
            logger.info(f"Patient {patient['id']} opted out")
            return "You've been unsubscribed from recall messages. Reply START to opt back in."
        
        elif intent == "START":
            # Opt back in
            supabase.table("patients").update({
                "opted_out": False,
                "opted_in_at": datetime.utcnow().isoformat()
            }).eq("id", patient["id"]).execute()
            
            logger.info(f"Patient {patient['id']} opted back in")
            return f"Welcome back! You're now subscribed to recall messages from {tenant['name']}."
        
        elif intent == "BOOK" and recall:
            # Mark recall as booked
            supabase.table("recalls").update({
                "status": "booked",
                "booked_at": datetime.utcnow().isoformat()
            }).eq("id", recall["id"]).execute()
            
            # Create booking record
            supabase.table("bookings").insert({
                "tenant_id": patient["tenant_id"],
                "patient_id": patient["id"],
                "recall_id": recall["id"],
                "source": "sms_reply",
                "status": "pending_confirmation"
            }).execute()
            
            logger.info(f"Recall {recall['id']} marked as booked via SMS")
            return f"Perfect! We've noted you'd like to book an appointment. Someone from {tenant['name']} will call you soon to confirm a time."
        
        elif intent == "SNOOZE" and recall:
            # Snooze for 14 days
            snooze_until = datetime.utcnow() + timedelta(days=14)
            supabase.table("recalls").update({
                "status": "snoozed",
                "snoozed_until": snooze_until.date().isoformat(),
                "next_send_at": snooze_until.isoformat()
            }).eq("id", recall["id"]).execute()
            
            logger.info(f"Recall {recall['id']} snoozed until {snooze_until.date()}")
            return "No problem! We'll remind you again in 2 weeks. 👍"
        
        else:
            # Unknown intent or no active recall
            if not recall:
                return f"Thanks for your message! We don't have any active recalls for you right now. Call {tenant.get('phone_number', 'us')} if you need to book."
            else:
                return f"Thanks for your message! Reply BOOK to schedule, LATER to remind you in 2 weeks, or STOP to unsubscribe."
    
    except Exception as e:
        logger.error(f"Error handling inbound SMS: {e}")
        return "We received your message but encountered an error. Please call us directly."

# ============================================================
# RECALL PROCESSOR (FIXED WITH ERROR HANDLING)
# ============================================================
def _process_single_recall(recall: dict):
    """Process a single recall - send next message in sequence"""
    patient = recall["patients"]
    tenant = recall["tenants"]
    template = recall.get("recall_templates")
    
    # Skip if patient opted out
    if patient.get("opted_out"):
        logger.info(f"Skipping recall {recall['id']} - patient opted out")
        supabase.table("recalls").update({"status": "opted_out"}).eq("id", recall["id"]).execute()
        return
    
    # Get message sequence
    if template and template.get("message_sequence"):
        sequence = template["message_sequence"]
    else:
        logger.warning(f"No template sequence for recall {recall['id']}")
        return
    
    step = recall.get("sequence_step", 0)
    
    if step >= len(sequence):
        # End of sequence - mark as completed
        supabase.table("recalls").update({"status": "completed"}).eq("id", recall["id"]).execute()
        logger.info(f"Recall {recall['id']} completed (end of sequence)")
        return
    
    # Get current message
    msg_config = sequence[step]
    message_text = render_template(msg_config["message"], patient, tenant, recall)
    
    # Send SMS
    result = send_sms(
        to=patient["phone"],
        body=message_text,
        tenant=tenant,
        recall_id=recall["id"],
        patient_id=patient["id"],
        sequence_step=step
    )
    
    # Calculate next send time
    next_step = step + 1
    if next_step < len(sequence):
        next_delay_days = sequence[next_step].get("delay_days", 7)
        next_send_at = datetime.utcnow() + timedelta(days=next_delay_days)
        new_status = "in_progress"
    else:
        next_send_at = None
        new_status = "in_progress"  # Will be marked completed on next run
    
    # Update recall
    updates = {
        "sequence_step": next_step,
        "next_send_at": next_send_at.isoformat() if next_send_at else None,
        "status": new_status if result["status"] == "sent" else "failed"
    }
    
    supabase.table("recalls").update(updates).eq("id", recall["id"]).execute()
    logger.info(f"Processed recall {recall['id']}, sent step {step}, status: {result['status']}")

def process_due_recalls():
    """Find all recalls due to send next message and dispatch SMS (FIXED WITH ERROR HANDLING)"""
    now = datetime.utcnow().isoformat()
    
    try:
        # Fetch recalls due for next message
        res = supabase.table("recalls").select(
            "*, patients(*), tenants(*), recall_templates(*)"
        ).in_("status", ["pending", "in_progress", "snoozed"]).lte("next_send_at", now).execute()
        
        recalls = res.data or []
        logger.info(f"Found {len(recalls)} recalls to process")
        
        success_count = 0
        error_count = 0
        
        for recall in recalls:
            try:
                _process_single_recall(recall)
                success_count += 1
            except Exception as e:
                error_count += 1
                logger.error(f"Failed to process recall {recall['id']}: {e}")
                
                # Mark recall as failed
                try:
                    supabase.table("recalls").update({
                        "status": "failed",
                        "notes": f"Processing error: {str(e)[:200]}"
                    }).eq("id", recall["id"]).execute()
                except:
                    logger.error(f"Failed to update failed status for recall {recall['id']}")
        
        logger.info(f"Recall processing complete: {success_count} success, {error_count} errors")
        return {"success": success_count, "errors": error_count, "total": len(recalls)}
    
    except Exception as e:
        logger.error(f"Fatal error in process_due_recalls: {e}")
        raise

# ============================================================
# ENDPOINTS
# ============================================================

# -- Health Check (IMPROVED) --
@app.get("/health")
def health_check():
    """Health check with DB and Twilio status"""
    health_status = {"status": "healthy", "timestamp": datetime.utcnow().isoformat(), "version": "2.0.0"}
    
    # Test DB
    try:
        supabase.table("tenants").select("id").limit(1).execute()
        health_status["database"] = "connected"
    except Exception as e:
        health_status["database"] = "error"
        health_status["status"] = "degraded"
        logger.error(f"DB health check failed: {e}")
    
    # Test Twilio
    try:
        twilio.api.accounts(TWILIO_SID).fetch()
        health_status["twilio"] = "connected"
    except Exception as e:
        health_status["twilio"] = "error"
        health_status["status"] = "degraded"
        logger.error(f"Twilio health check failed: {e}")
    
    return health_status

# -- Tenants (FIXED) --
@app.post("/tenants")
def create_tenant(config: TenantConfig, x_api_key: str = Header(...)):
    """Create a new tenant with auto-generated API key"""
    verify_api_key(x_api_key)
    
    # Generate unique API key for tenant
    tenant_api_key = f"sk_{config.slug}_{secrets.token_urlsafe(32)}"
    
    data = {
        "name": config.name,
        "slug": config.slug,
        "service_type": config.service_type,
        "phone_number": config.phone_number,
        "timezone": config.timezone,
        "country_code": config.country_code,
        "twilio_from": config.twilio_from,
        "settings": config.settings,
        "api_key": tenant_api_key,
        "active": True
    }
    
    try:
        res = supabase.table("tenants").insert(data).execute()
        logger.info(f"Created tenant: {res.data[0]['id']}")
        return res.data[0]
    except Exception as e:
        logger.error(f"Failed to create tenant: {e}")
        raise HTTPException(500, f"Failed to create tenant: {str(e)}")

@app.get("/tenants/{tenant_id}")
def get_tenant_info(tenant_id: str, tenant: dict = Depends(verify_tenant_auth)):
    """Get tenant information"""
    return tenant

# -- Templates --
@app.get("/templates")
def list_templates(service_type: Optional[str] = None):
    """List global recall templates"""
    query = supabase.table("recall_templates").select("*").is_("tenant_id", "null")
    if service_type:
        query = query.eq("service_type", service_type)
    res = query.order("service_type").execute()
    return {"data": res.data or []}

@app.get("/tenants/{tenant_id}/templates")
def list_tenant_templates(tenant_id: str, tenant: dict = Depends(verify_tenant_auth)):
    """List tenant-specific templates + global templates"""
    # Get tenant templates
    tenant_res = supabase.table("recall_templates").select("*").eq("tenant_id", tenant_id).execute()
    
    # Get global templates for this service type
    global_res = supabase.table("recall_templates").select("*").is_("tenant_id", "null").eq(
        "service_type", tenant["service_type"]
    ).execute()
    
    return {"data": (tenant_res.data or []) + (global_res.data or [])}

# -- Patients (FIXED WITH AUTH) --
@app.post("/tenants/{tenant_id}/patients")
def create_patient(tenant_id: str, patient: PatientCreate, tenant: dict = Depends(verify_tenant_auth)):
    """Create a new patient"""
    data = {
        "tenant_id": tenant_id,
        "external_id": patient.external_id,
        "first_name": patient.first_name,
        "last_name": patient.last_name,
        "phone": patient.phone,
        "email": patient.email,
        "date_of_birth": patient.date_of_birth.isoformat() if patient.date_of_birth else None,
        "metadata": patient.metadata,
    }
    
    try:
        res = supabase.table("patients").insert(data).execute()
        logger.info(f"Created patient: {res.data[0]['id']}")
        return res.data[0]
    except Exception as e:
        logger.error(f"Failed to create patient: {e}")
        raise HTTPException(500, f"Failed to create patient: {str(e)}")

@app.get("/tenants/{tenant_id}/patients")
def list_patients(
    tenant_id: str,
    tenant: dict = Depends(verify_tenant_auth),
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
):
    """List patients with optional search"""
    query = supabase.table("patients").select("*").eq("tenant_id", tenant_id)
    
    if search:
        # Search in name or phone
        query = query.or_(f"first_name.ilike.%{search}%,last_name.ilike.%{search}%,phone.ilike.%{search}%")
    
    res = query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
    return {"data": res.data or [], "total": len(res.data or [])}

# -- Recalls (FIXED WITH AUTH) --
@app.post("/tenants/{tenant_id}/recalls")
def create_recall(tenant_id: str, recall: RecallCreate, tenant: dict = Depends(verify_tenant_auth)):
    """Create a new recall"""
    
    # Validate template if provided
    if recall.template_id:
        t_res = supabase.table("recall_templates").select("*").eq("id", recall.template_id).single().execute()
        if not t_res.data:
            raise HTTPException(404, "Template not found")
    
    # Calculate first send time
    due_dt = datetime.combine(recall.due_date, datetime.min.time())
    today_dt = datetime.utcnow()
    first_send = max(due_dt, today_dt)
    
    data = {
        "tenant_id": tenant_id,
        "patient_id": recall.patient_id,
        "template_id": recall.template_id,
        "recall_type": recall.recall_type,
        "last_appointment": recall.last_appointment.isoformat() if recall.last_appointment else None,
        "due_date": recall.due_date.isoformat(),
        "status": "pending",
        "sequence_step": 0,
        "next_send_at": first_send.isoformat(),
        "booking_link": recall.booking_link,
        "notes": recall.notes,
        "priority": recall.priority,
    }
    
    try:
        res = supabase.table("recalls").insert(data).execute()
        logger.info(f"Created recall: {res.data[0]['id']}")
        return res.data[0]
    except Exception as e:
        logger.error(f"Failed to create recall: {e}")
        raise HTTPException(500, f"Failed to create recall: {str(e)}")

@app.get("/tenants/{tenant_id}/recalls")
def list_recalls(
    tenant_id: str,
    tenant: dict = Depends(verify_tenant_auth),
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
):
    """List recalls with optional status filter"""
    query = supabase.table("recalls").select("*, patients(first_name, last_name, phone)").eq("tenant_id", tenant_id)
    
    if status:
        query = query.eq("status", status)
    
    res = query.order("due_date", desc=False).range(offset, offset + limit - 1).execute()
    return {"data": res.data or [], "total": len(res.data or [])}

@app.get("/tenants/{tenant_id}/recalls/{recall_id}")
def get_recall(tenant_id: str, recall_id: str, tenant: dict = Depends(verify_tenant_auth)):
    """Get a specific recall"""
    res = supabase.table("recalls").select(
        "*, patients(*), recall_templates(*)"
    ).eq("id", recall_id).eq("tenant_id", tenant_id).single().execute()
    
    if not res.data:
        raise HTTPException(404, "Recall not found")
    
    return res.data

@app.patch("/tenants/{tenant_id}/recalls/{recall_id}")
def update_recall(
    tenant_id: str,
    recall_id: str,
    updates: Dict[str, Any],
    tenant: dict = Depends(verify_tenant_auth)
):
    """Update a recall"""
    allowed = {"status", "notes", "booking_link", "snoozed_until", "booked_at", "priority"}
    updates = {k: v for k, v in updates.items() if k in allowed}
    
    if not updates:
        raise HTTPException(400, "No valid fields to update")
    
    res = supabase.table("recalls").update(updates).eq("id", recall_id).eq("tenant_id", tenant_id).execute()
    
    if not res.data:
        raise HTTPException(404, "Recall not found")
    
    return res.data[0]

# -- Bulk Import (FIXED WITH AUTH) --
@app.post("/tenants/{tenant_id}/recalls/bulk-import")
def bulk_import(
    tenant_id: str,
    payload: BulkRecallImport,
    background_tasks: BackgroundTasks,
    tenant: dict = Depends(verify_tenant_auth)
):
    """Bulk import patients and create recalls"""
    background_tasks.add_task(_bulk_import_task, tenant_id, payload)
    logger.info(f"Queued bulk import of {len(payload.patients)} patients for tenant {tenant_id}")
    return {
        "message": f"Bulk import of {len(payload.patients)} patients queued for processing",
        "count": len(payload.patients)
    }

def _bulk_import_task(tenant_id: str, payload: BulkRecallImport):
    """Background task for bulk import"""
    tenant = get_tenant(tenant_id)
    template = None
    
    if payload.template_id:
        t_res = supabase.table("recall_templates").select("*").eq("id", payload.template_id).single().execute()
        template = t_res.data
    
    success_count = 0
    error_count = 0
    
    for raw in payload.patients:
        try:
            # Upsert patient
            phone = raw.get("phone", "")
            pat_data = {
                "tenant_id": tenant_id,
                "external_id": raw.get("external_id"),
                "first_name": raw["first_name"],
                "last_name": raw["last_name"],
                "phone": phone,
                "email": raw.get("email"),
                "metadata": raw.get("metadata", {}),
            }
            pat_res = supabase.table("patients").upsert(pat_data, on_conflict="tenant_id,phone").execute()
            patient = pat_res.data[0]
            
            # Calculate due date
            last_appt = raw.get("last_appointment")
            if last_appt:
                last_dt = datetime.strptime(last_appt, "%Y-%m-%d")
                due_date = last_dt + timedelta(days=payload.recall_interval_days)
            else:
                due_date = datetime.utcnow() + timedelta(days=payload.recall_interval_days)
            
            # Create recall
            first_send = max(due_date, datetime.utcnow())
            recall_data = {
                "tenant_id": tenant_id,
                "patient_id": patient["id"],
                "template_id": payload.template_id,
                "recall_type": raw.get("recall_type", "recall"),
                "last_appointment": last_appt,
                "due_date": due_date.date().isoformat(),
                "status": "pending",
                "sequence_step": 0,
                "next_send_at": first_send.isoformat(),
                "booking_link": raw.get("booking_link"),
            }
            supabase.table("recalls").insert(recall_data).execute()
            success_count += 1
            
        except Exception as e:
            error_count += 1
            logger.error(f"Bulk import failed for patient {raw.get('phone')}: {e}")
    
    logger.info(f"Bulk import complete: {success_count} success, {error_count} errors")

# -- Manual Send (FIXED WITH AUTH) --
@app.post("/tenants/{tenant_id}/recalls/{recall_id}/send-now")
def manual_send(tenant_id: str, recall_id: str, tenant: dict = Depends(verify_tenant_auth)):
    """Manually trigger sending of a recall"""
    recall_res = supabase.table("recalls").select(
        "*, patients(*), tenants(*), recall_templates(*)"
    ).eq("id", recall_id).eq("tenant_id", tenant_id).single().execute()
    
    if not recall_res.data:
        raise HTTPException(404, "Recall not found")
    
    try:
        _process_single_recall(recall_res.data)
        return {"message": "Recall sent successfully", "recall_id": recall_id}
    except Exception as e:
        logger.error(f"Manual send failed: {e}")
        raise HTTPException(500, f"Failed to send recall: {str(e)}")

# -- Analytics (FIXED WITH AUTH) --
@app.get("/tenants/{tenant_id}/analytics")
def get_analytics(tenant_id: str, tenant: dict = Depends(verify_tenant_auth), days: int = 30):
    """Get analytics for tenant"""
    try:
        # Get recall stats
        stats_res = supabase.rpc("get_recall_stats", {"p_tenant_id": tenant_id, "p_days": days}).execute()
        
        # Get SMS stats
        cutoff_date = (datetime.utcnow() - timedelta(days=days)).isoformat()
        sms_res = supabase.table("sms_messages").select("status").eq("tenant_id", tenant_id).gte(
            "created_at", cutoff_date
        ).execute()
        
        sms_stats = {
            "sent": sum(1 for m in (sms_res.data or []) if m.get("status") == "sent"),
            "delivered": sum(1 for m in (sms_res.data or []) if m.get("status") == "delivered"),
            "failed": sum(1 for m in (sms_res.data or []) if m.get("status") in ["failed", "undelivered"]),
        }
        
        # Get bookings and revenue
        bookings_res = supabase.table("bookings").select("id, revenue_amount").eq("tenant_id", tenant_id).gte(
            "created_at", cutoff_date
        ).execute()
        
        bookings = bookings_res.data or []
        revenue = sum(float(b.get("revenue_amount") or 0) for b in bookings)
        
        return {
            "recall_stats": stats_res.data or {},
            "sms_stats": sms_stats,
            "revenue_recovered": revenue,
            "bookings_count": len(bookings),
            "period_days": days
        }
    except Exception as e:
        logger.error(f"Analytics error: {e}")
        raise HTTPException(500, f"Failed to fetch analytics: {str(e)}")

# -- Cron Endpoint (ADMIN ONLY) --
@app.post("/cron/process-recalls", dependencies=[Depends(verify_api_key)])
def cron_process_recalls(background_tasks: BackgroundTasks):
    """Trigger recall processing (called by cron)"""
    background_tasks.add_task(process_due_recalls)
    return {"message": "Recall processing started", "timestamp": datetime.utcnow().isoformat()}

# -- Twilio Webhooks --
@app.post("/webhooks/twilio/inbound", response_class=PlainTextResponse)
async def twilio_inbound(request: Request):
    """Handle inbound SMS from Twilio"""
    url = str(request.url)
    form = await request.form()
    signature = request.headers.get("X-Twilio-Signature", "")
    
    # Validate Twilio signature in production
    if ENVIRONMENT == "production":
        if not twilio_validator.validate(url, dict(form), signature):
            logger.warning(f"Invalid Twilio signature from {request.client.host}")
            raise HTTPException(403, "Invalid Twilio signature")
    
    from_num = form.get("From", "")
    to_num = form.get("To", "")
    body = form.get("Body", "")
    sid = form.get("MessageSid", "")
    
    logger.info(f"Inbound SMS from {from_num}: {body[:50]}")
    
    reply = handle_inbound_sms(from_num, to_num, body, sid)
    
    # Return TwiML
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{reply}</Message></Response>'

@app.post("/webhooks/twilio/status")
async def twilio_status(request: Request):
    """Handle Twilio status callbacks"""
    form = await request.form()
    twilio_sid = form.get("MessageSid")
    status = form.get("MessageStatus")
    error_code = form.get("ErrorCode")
    
    if twilio_sid and status:
        try:
            supabase.table("sms_messages").update({
                "status": status,
                "error_code": error_code,
                "status_updated_at": datetime.utcnow().isoformat(),
            }).eq("twilio_sid", twilio_sid).execute()
            logger.info(f"Updated SMS status: {twilio_sid} -> {status}")
        except Exception as e:
            logger.error(f"Failed to update SMS status: {e}")
    
    return {"ok": True}

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)

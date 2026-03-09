
# ============================================================
# RECALL SaaS — FastAPI Backend (PRODUCTION VERSION)
# Healthcare-agnostic automated SMS recall engine
# Stack: FastAPI + Supabase + Twilio
# SCHEMA v1.0.0 COMPLIANT ✅
# ============================================================

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, validator
from typing import Optional, List, Dict, Any
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
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

# Rate limiting
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

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
# APP WITH RATE LIMITING
# ============================================================
app = FastAPI(
    title="Recall SaaS API",
    description="Healthcare-agnostic automated patient recall via SMS",
    version="2.0.0",
)

# Rate limiter setup
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if ENVIRONMENT == "development" else os.getenv("ALLOWED_ORIGINS", "").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- SERVE THE LANDING PAGE ---
if os.path.exists("website"):
    app.mount("/assets", StaticFiles(directory="website"), name="static")

# ============================================================
# MODELS
# ============================================================
class RecallStatus(str, Enum):
    pending     = "pending"
    in_progress = "in_progress"
    booked      = "booked"
    completed   = "completed"
    opted_out   = "opted_out"
    failed      = "failed"
    snoozed     = "snoozed"
    cancelled   = "cancelled"

class PatientCreate(BaseModel):
    external_id:    Optional[str]   = None
    first_name:     str
    last_name:      str
    preferred_name: Optional[str]   = None
    phone:          str
    email:          Optional[str]   = None
    date_of_birth:  Optional[date]  = None
    gender:         Optional[str]   = None
    communication_preferences: Dict[str, Any] = {
        "sms": True, 
        "email": False, 
        "whatsapp": False, 
        "preferred_time": "anytime", 
        "do_not_disturb": False
    }
    metadata:       Dict[str, Any]  = {}

    @validator("phone")
    def validate_phone(cls, v):
        if not re.match(r"^\+\d{7,15}$", v):
            raise ValueError("Phone must be E.164 format: +61412345678")
        return v

class TenantConfig(BaseModel):
    name:           str
    slug:           str
    service_type:   str
    phone_number:   Optional[str]   = None
    timezone:       str             = "Australia/Sydney"
    country_code:   str             = "AU"
    twilio_sid:     Optional[str]   = None
    twilio_token:   Optional[str]   = None
    twilio_from:    Optional[str]   = None
    settings:       Dict[str, Any]  = {}

class RecallCreate(BaseModel):
    patient_id:      str
    template_id:     Optional[str]  = None
    recall_type:     str
    last_appointment: Optional[date] = None
    due_date:        date
    booking_link:    Optional[str]  = None
    notes:           Optional[str]  = None
    priority:        int            = Field(default=1, ge=1, le=5)

    @validator("due_date")
    def validate_due_date(cls, v):
        if v < date.today():
            raise ValueError("Due date cannot be in the past")
        return v

class BulkImportRequest(BaseModel):
    patients:        List[Dict[str, Any]]
    template_id:     Optional[str] = None
    recall_type:     str
    recall_interval_days: int = 180

# ============================================================
# AUTH & SECURITY
# ============================================================
def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_SECRET:
        raise HTTPException(401, "Invalid API key")
    return True

def verify_tenant_auth(tenant_id: str, authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Invalid authorization header format. Use 'Bearer <token>'")
    
    try:
        token = authorization.replace("Bearer ", "").strip()
        
        tenant_res = supabase.table("tenants").select("*").eq("id", tenant_id).single().execute()
        tenant = tenant_res.data
        
        if not tenant:
            raise HTTPException(404, "Tenant not found")
        
        if tenant.get("api_key") != token:
            raise HTTPException(401, "Invalid tenant API key")
        
        if not tenant.get("active", True):
            raise HTTPException(403, "Tenant account is inactive")
        
        return tenant
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Tenant auth error: {e}")
        raise HTTPException(401, "Authentication failed")

def get_tenant(tenant_id: str) -> dict:
    res = supabase.table("tenants").select("*").eq("id", tenant_id).single().execute()
    if not res.data:
        raise HTTPException(404, "Tenant not found")
    return res.data

def get_tenant_timezone(tenant: dict) -> ZoneInfo:
    tz_str = tenant.get("timezone", "Australia/Sydney")
    try:
        return ZoneInfo(tz_str)
    except Exception as e:
        logger.warning(f"Invalid timezone {tz_str}, falling back to UTC: {e}")
        return ZoneInfo("UTC")

# ============================================================
# SMS ENGINE
# ============================================================
def render_template(template: str, patient: dict, tenant: dict, recall: dict) -> str:
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
    
    # Extract from twilio_config JSONB logic
    tenant_twilio = tenant.get("twilio_config", {})
    from_number = tenant_twilio.get("from_number") or TWILIO_FROM
    custom_sid = tenant_twilio.get("sid")
    custom_token = tenant_twilio.get("token")

    try:
        # If tenant has custom credentials, use them. Otherwise use global.
        if custom_sid and custom_token:
            custom_client = TwilioClient(custom_sid, custom_token)
            msg = custom_client.messages.create(to=to, from_=from_number, body=body)
        else:
            msg = twilio.messages.create(to=to, from_=from_number, body=body)
            
        status = "sent"
        message_sid = msg.sid
        error_code = None
        error_msg = None
    except Exception as e:
        logger.error(f"SMS send failed to {to}: {e}")
        status = "failed"
        message_sid = None
        error_code = "SEND_ERROR"
        error_msg = str(e)

    # Log to DB
    log_entry = {
        "tenant_id":       tenant["id"],
        "patient_id":      patient_id,
        "recall_id":       recall_id,
        "message_sid":     message_sid,
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
    return {"status": status, "message_sid": message_sid}

# ============================================================
# INBOUND SMS HANDLER
# ============================================================
def detect_intent(body: str) -> str:
    body_lower = body.lower().strip()
    if any(kw in body_lower for kw in ["stop", "cancel", "unsubscribe", "remove", "opt out", "optout"]): return "STOP"
    if any(kw in body_lower for kw in ["start", "unstop", "subscribe", "opt in", "optin"]): return "START"
    if any(kw in body_lower for kw in ["book", "yes", "y", "1", "confirm", "schedule", "ok", "sure", "please"]): return "BOOK"
    if any(kw in body_lower for kw in ["later", "not now", "snooze", "remind me", "next month", "maybe"]): return "SNOOZE"
    return "UNKNOWN"

def handle_inbound_sms(from_num: str, to_num: str, body: str, sid: str) -> str:
    try:
        patient_res = supabase.table("patients").select("*").eq("phone", from_num).execute()
        
        if not patient_res.data:
            logger.warning(f"Inbound SMS from unknown number: {from_num}")
            return "Thanks for your message. We couldn't find your number in our system. Please contact your healthcare provider directly."
        
        patient = patient_res.data[0]
        tenant = get_tenant(patient["tenant_id"])
        intent = detect_intent(body)
        
        logger.info(f"Inbound SMS from {from_num}: intent={intent}, body='{body[:50]}'")
        
        recall_res = supabase.table("recalls").select("*").eq("patient_id", patient["id"]).in_(
            "status", ["pending", "in_progress", "snoozed"]
        ).order("created_at", desc=True).limit(1).execute()
        
        recall = recall_res.data[0] if recall_res.data else None
        
        # Log inbound message (using message_sid per schema)
        supabase.table("inbound_responses").insert({
            "tenant_id": patient["tenant_id"],
            "patient_id": patient["id"],
            "recall_id": recall["id"] if recall else None,
            "from_number": from_num,
            "to_number": to_num,
            "body": body,
            "intent": intent,
            "message_sid": sid,
            "handled": True,
            "received_at": datetime.utcnow().isoformat()
        }).execute()
        
        if intent == "STOP":
            supabase.table("patients").update({
                "opted_out": True,
                "opted_out_at": datetime.utcnow().isoformat()
            }).eq("id", patient["id"]).execute()
            
            if recall:
                supabase.table("recalls").update({"status": "opted_out"}).eq("patient_id", patient["id"]).in_("status", ["pending", "in_progress", "snoozed"]).execute()
            return "You've been opted out from all recall messages. Reply START anytime to opt back in."
        
        elif intent == "START":
            supabase.table("patients").update({
                "opted_out": False,
                "opted_in_at": datetime.utcnow().isoformat()
            }).eq("id", patient["id"]).execute()
            return f"Welcome back! You're now subscribed to recall messages from {tenant['name']}."
        
        elif intent == "BOOK" and recall:
            supabase.table("recalls").update({
                "status": "booked",
                "booked_at": datetime.utcnow().isoformat()
            }).eq("id", recall["id"]).execute()
            
            supabase.table("bookings").insert({
                "tenant_id": patient["tenant_id"],
                "patient_id": patient["id"],
                "recall_id": recall["id"],
                "source": "sms_reply",
                "status": "confirmed" # updated from pending_confirmation to match booking_status_enum
            }).execute()
            return f"Perfect! We've noted you'd like to book an appointment. Someone from {tenant['name']} will call you soon to confirm a time."
        
        elif intent == "SNOOZE" and recall:
            snooze_until = datetime.utcnow() + timedelta(days=14)
            supabase.table("recalls").update({
                "status": "snoozed",
                "snoozed_until": snooze_until.date().isoformat(),
                "next_send_at": snooze_until.isoformat()
            }).eq("id", recall["id"]).execute()
            return "No problem! We'll remind you again in 2 weeks. 👍"
        
        else:
            if not recall:
                return f"Thanks for your message! We don't have any active recalls for you right now. Call {tenant.get('phone_number', 'us')} if you need to book."
            else:
                return f"Thanks for your message! Reply BOOK to schedule, LATER to remind you in 2 weeks, or STOP to unsubscribe."
    
    except Exception as e:
        logger.error(f"Error handling inbound SMS: {e}")
        return "We received your message but encountered an error. Please call us directly."

# ============================================================
# RECALL PROCESSOR
# ============================================================
def _process_single_recall(recall: dict):
    patient = recall["patients"]
    tenant = recall["tenants"]
    template = recall.get("recall_templates")
    
    if patient.get("opted_out"):
        logger.info(f"Skipping recall {recall['id']} - patient opted out")
        supabase.table("recalls").update({"status": "opted_out"}).eq("id", recall["id"]).execute()
        return
    
    if template and template.get("message_sequence"):
        sequence = template["message_sequence"]
    else:
        logger.warning(f"No template sequence for recall {recall['id']}")
        return
    
    step = recall.get("sequence_step", 0)
    
    if step >= len(sequence):
        supabase.table("recalls").update({"status": "completed"}).eq("id", recall["id"]).execute()
        logger.info(f"Recall {recall['id']} completed")
        return
    
    msg_config = sequence[step]
    message_text = render_template(msg_config["message_template"], patient, tenant, recall) # Updated key match DB
    
    result = send_sms(
        to=patient["phone"],
        body=message_text,
        tenant=tenant,
        recall_id=recall["id"],
        patient_id=patient["id"],
        sequence_step=step
    )
    
    next_step = step + 1
    if next_step < len(sequence):
        next_delay_days = sequence[next_step].get("delay_days", 7)
        tenant_tz = get_tenant_timezone(tenant)
        now_local = datetime.now(tenant_tz)
        next_send_local = now_local + timedelta(days=next_delay_days)
        next_send_local = next_send_local.replace(hour=9, minute=0, second=0, microsecond=0)
        next_send_at = next_send_local.astimezone(ZoneInfo("UTC"))
        new_status = "in_progress"
    else:
        next_send_at = None
        new_status = "in_progress"
    
    updates = {
        "sequence_step": next_step,
        "next_send_at": next_send_at.isoformat() if next_send_at else None,
        "status": new_status if result["status"] == "sent" else "failed",
        "last_sent_at": datetime.utcnow().isoformat(),
        "messages_sent": recall.get("messages_sent", 0) + 1
    }
    
    supabase.table("recalls").update(updates).eq("id", recall["id"]).execute()

def process_due_recalls():
    now = datetime.utcnow().isoformat()
    try:
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
                try:
                    supabase.table("recalls").update({
                        "status": "failed",
                        "notes": f"Processing error: {str(e)[:200]}"
                    }).eq("id", recall["id"]).execute()
                except Exception as update_error:
                    logger.error(f"Failed to update failed status for recall {recall['id']}: {update_error}")
        
        return {"success": success_count, "errors": error_count, "total": len(recalls)}
    except Exception as e:
        logger.error(f"Fatal error in process_due_recalls: {e}")
        raise

# ============================================================
# ENDPOINTS
# ============================================================

@app.get("/")
async def serve_landing():
    if os.path.exists("website/recall.html"):
        return FileResponse('website/recall.html')
    return {"service": "Recall SaaS API", "version": "2.0.0", "status": "operational"}

@app.get("/health")
def health_check():
    health_status = {
        "status": "healthy",
        "database": "unknown",
        "twilio": "unknown",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "2.0.0"
    }
    try:
        supabase.table("tenants").select("id").limit(1).execute()
        health_status["database"] = "connected"
    except Exception as e:
        health_status["database"] = "error"
        health_status["status"] = "degraded"
    
    try:
        twilio.api.accounts(TWILIO_SID).fetch()
        health_status["twilio"] = "connected"
    except Exception as e:
        health_status["twilio"] = "error"
        health_status["status"] = "degraded"
    
    return health_status

@app.post("/tenants")
@limiter.limit("10/minute")
def create_tenant(request: Request, config: TenantConfig, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    
    tenant_api_key = f"sk_{config.slug}_{secrets.token_urlsafe(32)}"
    
    # Construct JSONB config for twilio
    twilio_config = {}
    if config.twilio_sid: twilio_config["sid"] = config.twilio_sid
    if config.twilio_token: twilio_config["token"] = config.twilio_token
    if config.twilio_from: twilio_config["from_number"] = config.twilio_from
    
    data = {
        "name": config.name,
        "slug": config.slug,
        "service_type": config.service_type,
        "phone_number": config.phone_number,
        "timezone": config.timezone,
        "country_code": config.country_code,
        "twilio_config": twilio_config,
        "settings": config.settings,
        "api_key": tenant_api_key,
        "active": True
    }
    
    try:
        res = supabase.table("tenants").insert(data).execute()
        return res.data[0]
    except Exception as e:
        raise HTTPException(500, f"Failed to create tenant: {str(e)}")

@app.get("/tenants/{tenant_id}")
def get_tenant_info(tenant_id: str, tenant: dict = Depends(verify_tenant_auth)):
    return tenant

@app.get("/templates")
def list_templates(service_type: Optional[str] = None):
    query = supabase.table("recall_templates").select("*").is_("tenant_id", "null")
    if service_type:
        query = query.eq("service_type", service_type)
    res = query.order("service_type").execute()
    return {"data": res.data or []}

@app.get("/tenants/{tenant_id}/templates")
def list_tenant_templates(tenant_id: str, tenant: dict = Depends(verify_tenant_auth)):
    tenant_res = supabase.table("recall_templates").select("*").eq("tenant_id", tenant_id).execute()
    global_res = supabase.table("recall_templates").select("*").is_("tenant_id", "null").eq(
        "service_type", tenant["service_type"]
    ).execute()
    return {"data": (tenant_res.data or []) + (global_res.data or [])}

@app.post("/tenants/{tenant_id}/patients")
@limiter.limit("100/minute")
def create_patient(request: Request, tenant_id: str, patient: PatientCreate, tenant: dict = Depends(verify_tenant_auth)):
    data = {
        "tenant_id": tenant_id,
        "external_id": patient.external_id,
        "first_name": patient.first_name,
        "last_name": patient.last_name,
        "preferred_name": patient.preferred_name,
        "phone": patient.phone,
        "email": patient.email,
        "date_of_birth": patient.date_of_birth.isoformat() if patient.date_of_birth else None,
        "gender": patient.gender,
        "communication_preferences": patient.communication_preferences,
        "metadata": patient.metadata,
    }
    try:
        res = supabase.table("patients").insert(data).execute()
        return res.data[0]
    except Exception as e:
        raise HTTPException(500, f"Failed to create patient: {str(e)}")

@app.get("/tenants/{tenant_id}/patients")
def list_patients(tenant_id: str, tenant: dict = Depends(verify_tenant_auth), search: Optional[str] = None, limit: int = 50, offset: int = 0):
    query = supabase.table("patients").select("*").eq("tenant_id", tenant_id)
    if search:
        query = query.or_(f"first_name.ilike.%{search}%,last_name.ilike.%{search}%,phone.ilike.%{search}%")
    res = query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
    return {"data": res.data or [], "total": len(res.data or [])}

@app.post("/tenants/{tenant_id}/recalls")
@limiter.limit("100/minute")
def create_recall(request: Request, tenant_id: str, recall: RecallCreate, tenant: dict = Depends(verify_tenant_auth)):
    if recall.template_id:
        t_res = supabase.table("recall_templates").select("*").eq("id", recall.template_id).single().execute()
        if not t_res.data:
            raise HTTPException(404, "Template not found")
    
    tenant_tz = get_tenant_timezone(tenant)
    due_dt_local = datetime.combine(recall.due_date, datetime.min.time()).replace(tzinfo=tenant_tz)
    due_dt_local = due_dt_local.replace(hour=9, minute=0, second=0, microsecond=0)
    first_send_utc = due_dt_local.astimezone(ZoneInfo("UTC"))
    now_utc = datetime.now(ZoneInfo("UTC"))
    if first_send_utc < now_utc:
        first_send_utc = now_utc
    
    data = {
        "tenant_id": tenant_id,
        "patient_id": recall.patient_id,
        "template_id": recall.template_id,
        "recall_type": recall.recall_type,
        "last_appointment": recall.last_appointment.isoformat() if recall.last_appointment else None,
        "due_date": recall.due_date.isoformat(),
        "status": "pending",
        "sequence_step": 0,
        "next_send_at": first_send_utc.isoformat(),
        "booking_link": recall.booking_link,
        "notes": recall.notes,
        "priority": recall.priority,
    }
    
    try:
        res = supabase.table("recalls").insert(data).execute()
        return res.data[0]
    except Exception as e:
        raise HTTPException(500, f"Failed to create recall: {str(e)}")

@app.get("/tenants/{tenant_id}/recalls")
def list_recalls(tenant_id: str, tenant: dict = Depends(verify_tenant_auth), status: Optional[str] = None, limit: int = 50, offset: int = 0):
    query = supabase.table("recalls").select("*, patients(first_name, last_name, phone)").eq("tenant_id", tenant_id)
    if status:
        query = query.eq("status", status)
    res = query.order("due_date", desc=False).range(offset, offset + limit - 1).execute()
    return {"data": res.data or [], "total": len(res.data or [])}

@app.get("/tenants/{tenant_id}/recalls/{recall_id}")
def get_recall(tenant_id: str, recall_id: str, tenant: dict = Depends(verify_tenant_auth)):
    res = supabase.table("recalls").select("*, patients(*), recall_templates(*)").eq("id", recall_id).eq("tenant_id", tenant_id).single().execute()
    if not res.data:
        raise HTTPException(404, "Recall not found")
    return res.data

@app.patch("/tenants/{tenant_id}/recalls/{recall_id}")
def update_recall(tenant_id: str, recall_id: str, updates: dict, tenant: dict = Depends(verify_tenant_auth)):
    try:
        res = supabase.table("recalls").update(updates).eq("id", recall_id).eq("tenant_id", tenant_id).execute()
        if not res.data:
            raise HTTPException(404, "Recall not found")
        return res.data[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to update recall: {str(e)}")

@app.post("/tenants/{tenant_id}/recalls/{recall_id}/send-now")
def send_recall_now(tenant_id: str, recall_id: str, tenant: dict = Depends(verify_tenant_auth)):
    recall_res = supabase.table("recalls").select("*, patients(*), recall_templates(*)").eq("id", recall_id).eq("tenant_id", tenant_id).single().execute()
    if not recall_res.data:
        raise HTTPException(404, "Recall not found")
    
    recall = recall_res.data
    recall["tenants"] = tenant
    try:
        _process_single_recall(recall)
        return {"message": "Recall sent successfully", "recall_id": recall_id}
    except Exception as e:
        raise HTTPException(500, f"Failed to send recall: {str(e)}")

@app.post("/tenants/{tenant_id}/recalls/bulk-import")
@limiter.limit("10/minute")
def bulk_import_recalls(request: Request, tenant_id: str, import_req: BulkImportRequest, background_tasks: BackgroundTasks, tenant: dict = Depends(verify_tenant_auth)):
    def _import_task():
        created_count = 0
        error_count = 0
        for patient_data in import_req.patients:
            try:
                patient_insert = {
                    "tenant_id": tenant_id,
                    "first_name": patient_data["first_name"],
                    "last_name": patient_data["last_name"],
                    "phone": patient_data["phone"],
                    "email": patient_data.get("email"),
                    "external_id": patient_data.get("external_id"),
                }
                patient_res = supabase.table("patients").upsert(patient_insert).execute()
                patient = patient_res.data[0]
                
                due_date_str = patient_data.get("due_date")
                if not due_date_str:
                    last_apt = datetime.fromisoformat(patient_data.get("last_appointment"))
                    due_date = last_apt.date() + timedelta(days=import_req.recall_interval_days)
                else:
                    due_date = datetime.fromisoformat(due_date_str).date()
                
                tenant_tz = get_tenant_timezone(tenant)
                first_send_local = datetime.combine(due_date, datetime.min.time()).replace(tzinfo=tenant_tz, hour=9)
                first_send_utc = first_send_local.astimezone(ZoneInfo("UTC"))
                
                recall_insert = {
                    "tenant_id": tenant_id,
                    "patient_id": patient["id"],
                    "template_id": import_req.template_id,
                    "recall_type": import_req.recall_type,
                    "due_date": due_date.isoformat(),
                    "status": "pending",
                    "next_send_at": first_send_utc.isoformat(),
                    "sequence_step": 0,
                }
                supabase.table("recalls").insert(recall_insert).execute()
                created_count += 1
            except Exception as e:
                error_count += 1
                logger.error(f"Bulk import error: {e}")
        logger.info(f"Bulk import completed: {created_count} created, {error_count} errors")
    
    background_tasks.add_task(_import_task)
    return {"message": f"Bulk import of {len(import_req.patients)} patients queued", "count": len(import_req.patients)}

@app.get("/tenants/{tenant_id}/analytics")
def get_analytics(tenant_id: str, tenant: dict = Depends(verify_tenant_auth), days: int = 30):
    start_date = (datetime.utcnow() - timedelta(days=days)).isoformat()
    try:
        res = supabase.rpc("get_recall_stats", {"p_tenant_id": tenant_id, "p_days": days}).execute()
        return res.data
    except Exception as e:
        logger.warning(f"Analytics function failed: {e}")
        return {"error": "Analytics data not available"}

@app.post("/cron/process-recalls")
@limiter.limit("10/minute")
def cron_process_recalls(request: Request, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    try:
        result = process_due_recalls()
        return {"message": "Recall processing completed", "timestamp": datetime.utcnow().isoformat(), **result}
    except Exception as e:
        raise HTTPException(500, f"Cron job failed: {str(e)}")

@app.post("/webhooks/twilio/inbound")
async def twilio_inbound_webhook(request: Request):
    form = await request.form()
    if ENVIRONMENT == "production":
        signature = request.headers.get("X-Twilio-Signature", "")
        url = str(request.url)
        if not twilio_validator.validate(url, dict(form), signature):
            raise HTTPException(403, "Invalid Twilio signature")
    
    from_num = form.get("From")
    to_num = form.get("To")
    body = form.get("Body", "")
    sid = form.get("MessageSid")
    
    response_text = handle_inbound_sms(from_num, to_num, body, sid)
    return PlainTextResponse(
        content=f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{response_text}</Message></Response>',
        media_type="text/xml"
    )

@app.post("/webhooks/twilio/status")
async def twilio_status_webhook(request: Request):
    form = await request.form()
    sid = form.get("MessageSid")
    status = form.get("MessageStatus")
    error_code = form.get("ErrorCode")
    
    try:
        supabase.table("sms_messages").update({
            "status": status,
            "error_code": error_code,
            "delivered_at": datetime.utcnow().isoformat() if status == "delivered" else None
        }).eq("message_sid", sid).execute()
    except Exception as e:
        logger.error(f"Failed to update SMS status: {e}")
    
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

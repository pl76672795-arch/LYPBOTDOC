from pydantic import BaseModel, Field
from typing import Optional

class UserSession(BaseModel):
    user_id: int
    client_name: str
    document_type: str
    client_dni: str = Field(..., min_length=8, max_length=11)
    monto: float = Field(0.0, ge=0)
    direccion: str = ""
    tipo_uso: str = "Vivienda"
    plazo_meses: int = Field(12, gt=0)
    garantia_meses: int = Field(1, ge=0)

class APIError(Exception): pass
class AIError(Exception): pass
class StorageError(Exception): pass
class DocumentError(Exception): pass
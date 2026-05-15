import dotenv from 'dotenv';
dotenv.config();

// Tokens y URLs oficiales de proveedores
// Se prefiere siempre la variable de entorno, pero se incluye un fallback temporal si es necesario
export const JSONPE_TOKEN = process.env.JSONPE_TOKEN || 'E38fc748aaf4c7e7a2d6515a944ea2facfb185ab556cf0ee6c7e1167996d';
export const BASE_URL_API = 'https://api.json.pe/api';
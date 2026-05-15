import axios from 'axios';
import { supabase } from '../config/supabase.js';
import { JSONPE_TOKEN, BASE_URL_API } from '../config/constants.js';

export const consultarRUC = async (ruc) => {
  try {
    // 1. REVISIÓN DE CACHÉ
    const ayer = new Date();
    ayer.setHours(ayer.getHours() - 24);

    const { data: cacheData, error: cacheError } = await supabase
      .from('consultas')
      .select('resultado')
      .eq('tipo', 'RUC')
      .eq('numero_documento', ruc)
      .gte('creado_at', ayer.toISOString())
      .order('creado_at', { ascending: false })
      .limit(1)
      .single();

    if (cacheData && !cacheError) {
      return { success: true, data: cacheData.resultado, source: 'cache' };
    }

    // 2. CONSUMO DE API EXTERNA
    const response = await axios.post(`${BASE_URL_API}/ruc`, { ruc }, {
      headers: {
        'Authorization': `Bearer ${JSONPE_TOKEN}`,
        'Content-Type': 'application/json'
      }
    });

    if (!response.data.success) {
      return { success: false, error: '⚠️ No se encontró información oficial para ese número. Por favor, verifíquelo.' };
    }

    const apiData = response.data.data;

    // 3. NORMALIZACIÓN DE DATOS (Mapeo estricto para contratos)
    const normalizedData = {
      razon_social: apiData.nombre_o_razon_social || '',
      estado: apiData.estado || '',
      condicion: apiData.condicion || '',
      direccion_fiscal: apiData.direccion_completa || 'No registrada'
    };

    // 4. ACTUALIZACIÓN DE CACHÉ
    await supabase
      .from('consultas')
      .insert([{ tipo: 'RUC', numero_documento: ruc, resultado: normalizedData }]);

    return { success: true, data: normalizedData, source: 'api' };
  } catch (error) {
    console.error('[SunatProvider] ⚠️ Error en consulta RUC:', error.message);
    return { success: false, error: '⚠️ No se encontró información oficial para ese número. Por favor, verifíquelo.' };
  }
};
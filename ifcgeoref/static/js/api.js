export class ApiError extends Error {
  constructor(message, status = 0, data = null) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

function messageFrom(data, status) {
  if (data && data.error) return typeof data.error === 'string' ? data.error : data.error.message;
  if (status === 0) return 'Cannot reach the server. Check your connection and try again.';
  return `The request failed (${status}).`;
}

async function call(method, url, { json, headers = {} } = {}) {
  const options = { method, headers: { ...headers } };
  if (json !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(json);
  }
  let response;
  try {
    response = await fetch(url, options);
  } catch (e) {
    throw new ApiError(messageFrom(null, 0), 0);
  }
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
  if (!response.ok) throw new ApiError(messageFrom(data, response.status), response.status, data);
  return data;
}

const q = (params) => new URLSearchParams(
  Object.entries(params).filter(([, v]) => v !== null && v !== undefined && v !== ''),
).toString();

export const api = {
  /** Upload with real byte progress (fetch cannot report upload progress). */
  upload(file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/jobs');
      xhr.responseType = 'text';
      xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
      xhr.onload = () => {
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new ApiError(messageFrom(data, xhr.status), xhr.status, data));
      };
      xhr.onerror = () => reject(new ApiError(messageFrom(null, 0), 0));
      const form = new FormData();
      form.append('file', file);
      xhr.send(form);
    });
  },
  status: (id) => call('GET', `/api/jobs/${id}`),
  solve: (id, payload) => call('POST', `/api/jobs/${id}/solve`, { json: payload }),
  georeference: (id, payload) => call('POST', `/api/jobs/${id}/georeference`, { json: payload }),
  uploadToProject: (id, projectId, token) => call('POST', `/api/jobs/${id}/upload`, {
    json: { project_id: projectId },
    headers: token ? { Authorization: `Token ${token}` } : {},
  }),
  suggest: (lat, lon) => call('GET', `/api/crs/suggest?${q({ lat, lon })}`),
  searchCrs: (text, lat, lon) => call('GET', `/api/crs/search?${q({ q: text, lat, lon })}`),
  crs: (code, lat, lon) => call('GET', `/api/crs/${code}?${q({ lat, lon })}`),
  toGrid: (code, points) => call('POST', `/api/crs/${code}/convert`, { json: { to: 'grid', points } }),
  toWgs84: (code, points) => call('POST', `/api/crs/${code}/convert`, { json: { to: 'wgs84', points } }),
  elevation: (lat, lon) => call('GET', `/api/elevation?${q({ lat, lon })}`),
};

export const modelUrl = (id) => `/api/jobs/${id}/model.glb`;
export const downloadUrl = (id) => `/api/jobs/${id}/download`;

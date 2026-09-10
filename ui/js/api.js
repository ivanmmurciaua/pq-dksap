// JSON POST to the local backend. Throws with the backend's error message so
// callers can surface it directly.
export async function api(path, body){
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const j = await r.json();
  if(!r.ok) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}

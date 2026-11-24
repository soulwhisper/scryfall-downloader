const labels = {ok: "Ready", unavailable: "Unavailable", not_writable: "Not writable", pending: "Not checked"};
const timer = new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short", timeZone: "UTC"});

export const $ = (id) => document.getElementById(id);
export const utc = (value) => value ? `${timer.format(new Date(value))} UTC` : "—";

export function node(tag, text, className) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  if (className) item.className = className;
  return item;
}

export function badge(state, text) {
  const item = node("span", text || labels[state] || state, "badge");
  item.dataset.state = state;
  return item;
}

export function button(text, action) {
  const item = node("button", text);
  item.type = "button";
  item.addEventListener("click", action);
  return item;
}

export function link(text, href) {
  const item = node("a", text);
  item.href = href;
  return item;
}

export function stat(label, value) {
  const item = node("div");
  const data = node("dd");
  data.append(value);
  item.append(node("dt", label), data);
  return item;
}

export function feedback(message = "", error = false) {
  $("notice").hidden = !message;
  $("notice").textContent = message;
  $("notice").dataset.state = error ? "error" : "info";
}

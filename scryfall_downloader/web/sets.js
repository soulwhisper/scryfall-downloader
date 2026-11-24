import { $, button, node } from "./ui.js";

export class SetPicker {
  constructor(prefix, limit, changed, reloadCatalogue) {
    this.limit = limit;
    this.changed = changed;
    this.codes = [];
    this.rows = [];
    this.count = 12;
    this.root = $(`${prefix}-picker`);
    this.selected = node("ul", undefined, "selected-sets");
    this.selected.setAttribute("aria-label", "Selected sets");
    this.query = node("input");
    this.query.type = "search";
    this.query.id = `${prefix}-search`;
    this.query.placeholder = "Name or code, e.g. MSH";
    this.query.autocomplete = "off";
    this.filter = node("select");
    this.filter.id = `${prefix}-filter`;
    for (const [value, text] of [["all", "All types"], ["paper", "Paper sets"], ["token", "Token sets"], ["digital", "Digital sets"]]) {
      const option = node("option", text);
      option.value = value;
      this.filter.append(option);
    }
    const controls = node("div", undefined, "picker-controls");
    const searchLabel = node("label", "Find a set"); searchLabel.append(this.query);
    const filterLabel = node("label", "Type"); filterLabel.append(this.filter);
    controls.append(searchLabel, filterLabel);
    this.selection = node("p", undefined, "caption");
    this.results = node("ul", undefined, "set-results");
    this.results.setAttribute("aria-label", "Matching sets");
    this.info = node("p", "Loading the catalogue…", "caption");
    this.more = button("Show more sets", () => { this.count += 24; this.drawMatches(); });
    this.more.hidden = true;
    this.retry = button("Reload catalogue", reloadCatalogue);
    this.retry.hidden = true;
    this.manual = button("", () => this.add(this.query.value.trim().toLowerCase()));
    this.manual.hidden = true;
    this.root.append(node("h3", "Select sets"), this.selection, this.selected, controls, this.info,
      this.results, this.more, this.manual, this.retry);
    this.query.addEventListener("input", () => { this.count = 12; this.drawMatches(); });
    this.filter.addEventListener("change", () => { this.count = 12; this.drawMatches(); });
    this.query.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      const code = this.query.value.trim().toLowerCase();
      if (/^[a-z0-9]{1,12}$/.test(code)) this.add(code);
    });
    this.drawSelection();
  }

  setCodes(codes) {
    this.codes = [...codes];
    this.drawSelection();
    this.drawMatches();
  }

  catalogue(rows, error = "") {
    this.rows = rows;
    this.failure = error;
    this.drawSelection();
    this.drawMatches();
  }

  add(code) {
    if (this.codes.includes(code) || this.codes.length >= this.limit) return;
    this.setCodes([...this.codes, code]);
    this.changed();
    this.query.focus();
  }

  drawSelection() {
    this.selection.textContent = `${this.codes.length} / ${this.limit} selected`;
    this.selected.replaceChildren(...this.codes.map((code) => {
      const item = node("li");
      const control = button(`${code.toUpperCase()} ×`, () => {
        this.setCodes(this.codes.filter((selected) => selected !== code));
        this.changed();
        this.query.focus();
      });
      const name = this.rows.find((row) => row.code === code)?.name;
      if (name) control.title = name;
      control.setAttribute("aria-label", `Remove ${code.toUpperCase()}`);
      item.append(control);
      return item;
    }));
  }

  drawMatches() {
    const query = this.query.value.trim().toLowerCase();
    const filter = this.filter.value;
    const matched = this.rows.filter((row) => {
      if (filter === "paper" && (row.digital || row.set_type === "token")) return false;
      if (filter === "token" && row.set_type !== "token") return false;
      if (filter === "digital" && !row.digital) return false;
      return !query || row.code.includes(query) || row.name.toLowerCase().includes(query) || row.parent_set_code === query;
    });
    const exact = matched.findIndex((row) => row.code === query);
    if (exact > 0) matched.unshift(...matched.splice(exact, 1));
    this.info.textContent = this.failure || (this.rows.length ? (matched.length ?
      `Showing ${Math.min(matched.length, this.count)} of ${matched.length} matching sets` : "No matching sets") : "Loading the catalogue…");
    this.more.hidden = matched.length <= this.count;
    this.retry.hidden = !this.failure;
    this.manual.hidden = !/^[a-z0-9]{1,12}$/.test(query) || this.rows.some((row) => row.code === query);
    this.manual.textContent = `Add exact code ${query.toUpperCase()}`;
    this.manual.disabled = this.codes.includes(query) || this.codes.length >= this.limit;
    this.results.replaceChildren(...matched.slice(0, this.count).map((row) => {
      const description = node("div");
      const meta = [row.code.toUpperCase(), row.set_type.replaceAll("_", " "), row.released_at];
      if (row.parent_set_code) meta.push(`child of ${row.parent_set_code.toUpperCase()}`);
      description.append(node("span", row.name, "set-name"), node("span", meta.filter(Boolean).join(" · "), "set-metadata"));
      const selected = this.codes.includes(row.code);
      const add = button(selected ? "Added" : "Add", () => this.add(row.code));
      add.setAttribute("aria-label", `${selected ? "Added" : "Add"} ${row.code.toUpperCase()}`);
      add.disabled = selected || this.codes.length >= this.limit;
      const item = node("li");
      item.append(description, add);
      return item;
    }));
  }
}

export function policyControls(prefix) {
  const quality = node("select");
  quality.id = `${prefix}-quality`;
  const qualities = [["", "No minimum · keep valid files"], ["small", "Small · 146 × 204"], ["normal", "Normal · 488 × 680"],
    ["large", "Large · 672 × 936"], ["png", "Native PNG · 744 × 1040"]];
  for (const [value, text] of qualities) { const option = node("option", text); option.value = value; quality.append(option); }
  const languages = node("input");
  languages.id = `${prefix}-languages`;
  languages.type = "text";
  languages.value = "zhs, en";
  languages.required = true;
  languages.spellcheck = false;
  const langLabel = node("label", "Languages in priority order");
  langLabel.append(languages, node("small", "Comma-separated codes. zhs = Simplified Chinese; en = English."));
  const qualityLabel = node("label", "Minimum quality"); qualityLabel.append(quality);
  const flags = [["refresh_language", "Replace with a verified preferred language"], ["refresh_scan", "Refresh when Scryfall has a newer scan"]];
  const controls = {};
  const root = $(`${prefix}-policy`);
  root.append(langLabel, qualityLabel);
  for (const [name, text] of flags) {
    const label = node("label", undefined, "check");
    controls[name] = node("input");
    controls[name].type = "checkbox";
    controls[name].id = `${prefix}-${name.replace("_", "-")}`;
    label.append(controls[name], text);
    root.append(label);
  }
  return {
    read: () => ({languages: languages.value.split(",").map((language) => language.trim()).filter(Boolean),
      min_quality: quality.value || null, refresh_language: controls.refresh_language.checked, refresh_scan: controls.refresh_scan.checked}),
    fill: (policy) => {
      languages.value = policy.languages.join(", ");
      quality.value = policy.min_quality || "";
      for (const [name] of flags) controls[name].checked = policy[name];
    },
  };
}

const modelConnectionFields = ["llm_base_url", "llm_model", "llm_protocol"];
let modelProfiles = [], editingModelId = null, deletingModel = null, modelProfilesVersion = 0;
let modelProfilesBusy = false;

function modelProfileStatus(text, error = false) {
  const status = $("#model-profile-status");
  status.textContent = text;
  status.classList.toggle("error", error);
}
function resetModelProfiles() {
  modelProfilesVersion += 1;
  modelProfiles = []; deletingModel = null;
  $("#model-profile-dialog").close(); $("#model-profile-delete").close();
  $("#model-profile-form").reset(); editingModelId = null;
  $("#model-profile-search").value = "";
  $("#model-profile-list").replaceChildren();
  modelProfileStatus("");
}
function renderModelProfiles() {
  const query = $("#model-profile-search").value.trim().toLocaleLowerCase();
  const matches = modelProfiles.filter(profile =>
    [profile.name, profile.llm_model, profile.llm_base_url].some(value => value.toLocaleLowerCase().includes(query)));
  const list = $("#model-profile-list"); list.replaceChildren();
  if (!matches.length) list.append(node("p", modelProfiles.length ? "沒有符合的模型設定。" : "尚未儲存模型設定。", "hint"));
  for (const profile of matches) {
    const item = node("article", undefined, "item");
    item.dataset.profileId = profile.id;
    item.append(node("h3", profile.name), node("p", profile.llm_model),
      node("p", profile.llm_base_url, "hint"));
    const protocol = [...$("#model-profile-form").elements.llm_protocol.options]
      .find(option => option.value === profile.llm_protocol)?.textContent || profile.llm_protocol;
    item.append(node("p", protocol + " · 金鑰已設定" + (profile.is_active ? " · 目前使用中" : ""), "hint"));
    const actions = node("div", undefined, "row");
    for (const [label, handler] of [
      ["套用", () => applyModelProfile(profile)],
      ["編輯", () => editModelProfile(profile)],
      ["刪除", () => {
        deletingModel = profile;
        $("#model-profile-delete-text").textContent = profile.name + " · " + profile.llm_model + " · " + profile.llm_base_url;
        $("#model-profile-delete").returnValue = "cancel";
        $("#model-profile-delete").showModal();
      }],
    ]) {
      const button = node("button", label); button.type = "button"; button.disabled = modelProfilesBusy;
      button.addEventListener("click", handler); actions.append(button);
    }
    item.append(actions); list.append(item);
  }
}
async function loadModelProfiles() {
  const version = ++modelProfilesVersion;
  try {
    const result = await api("/api/admin/model-profiles");
    if (version !== modelProfilesVersion) return;
    modelProfiles = result.items; renderModelProfiles();
  } catch (error) {
    if (version === modelProfilesVersion) modelProfileStatus("無法更新列表：" + error.message, true);
  }
}
function setModelProfilesBusy(busy) {
  modelProfilesBusy = busy;
  $("#add-model-profile").disabled = busy;
  $("#model-profile-form button[type=submit]").disabled = busy;
  renderModelProfiles();
}
function editModelProfile(profile = null) {
  if (modelProfilesBusy) return;
  const form = $("#model-profile-form"); form.reset();
  editingModelId = profile?.id || null;
  form.elements.name.value = profile?.name || "";
  for (const key of modelConnectionFields)
    form.elements[key].value = profile ? profile[key] : $("#services").elements[key].value;
  form.elements.llm_context_window.value = (profile ? profile.llm_context_window : Number($("#services").elements.llm_context_window.value)) || "";
  form.elements.llm_api_key.required = !profile;
  $("#model-profile-title").textContent = profile ? "編輯模型設定" : "新增模型設定";
  $("#model-profile-key-help").textContent = profile ? "已儲存金鑰不會顯示；留白保留原值，填入新金鑰即可替換。" : "請填入這組模型的金鑰。保存後可在列表按「套用」。";
  $("#model-profile-editor-status").textContent = "";
  $("#model-profile-dialog").showModal();
}
async function applyModelProfile(profile) {
  if (modelProfilesBusy) return;
  setModelProfilesBusy(true);
  try {
    const settings = await api("/api/admin/model-profiles/" + profile.id + "/apply", {method: "POST"});
    renderLLMSettings(settings);
    modelProfileStatus("已套用「" + profile.name + "」，可使用下方按鈕測試模型連線。");
    await loadModelProfiles();
  } catch (error) {
    if (csrfToken) modelProfileStatus(error.message, true);
  } finally { setModelProfilesBusy(false); }
}
$("#add-model-profile").addEventListener("click", () => editModelProfile());
$("#refresh-model-profiles").addEventListener("click", () => { modelProfileStatus(""); loadModelProfiles(); });
$("#model-profile-search").addEventListener("input", renderModelProfiles);
for (const id of ["close-model-profile", "cancel-model-profile"])
  $("#" + id).addEventListener("click", () => $("#model-profile-dialog").close());
$("#model-profile-dialog").addEventListener("close", () => {
  $("#model-profile-form").reset(); editingModelId = null;
});
$("#model-profile-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (modelProfilesBusy) return;
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form));
  values.llm_context_window = Number(values.llm_context_window) || 0;
  const ident = editingModelId;
  setModelProfilesBusy(true);
  $("#model-profile-editor-status").textContent = "正在保存…";
  try {
    await api("/api/admin/model-profiles" + (ident ? "/" + ident : ""), {
      method: ident ? "PUT" : "POST", body: JSON.stringify(values),
    });
    $("#model-profile-dialog").close();
    modelProfileStatus("模型設定已保存，按「套用」即可切換。");
    await loadModelProfiles();
  } catch (error) {
    if (csrfToken) $("#model-profile-editor-status").textContent = error.message;
  } finally { setModelProfilesBusy(false); }
});
$("#model-profile-delete").addEventListener("close", async () => {
  const profile = deletingModel; deletingModel = null;
  if (!profile || $("#model-profile-delete").returnValue !== "confirm" || modelProfilesBusy) return;
  setModelProfilesBusy(true);
  try {
    await api("/api/admin/model-profiles/" + profile.id, {method: "DELETE"});
    modelProfileStatus("已刪除「" + profile.name + "」，目前使用的模型不變。");
    await loadModelProfiles();
  } catch (error) {
    if (csrfToken) modelProfileStatus(error.message, true);
  } finally { setModelProfilesBusy(false); }
});

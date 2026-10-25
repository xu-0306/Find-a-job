"use strict";

const assert = require("node:assert/strict");
const {resumeReviewChecklist} = require("../app/static/cards.js");
const date = new Date(2026, 9, 25, 23, 59);
const data = {
  target: {type: "jd_text", label: "後端工程師"}, has_jd: true, group: "優先修改重點",
  prosecution: [{issue: "不應匯出的扣分", impact: "不應匯出的影響"}],
  defense: {responses: ["不應匯出的辯護"], strengths: ["不應匯出的優勢"]},
  fixes: [{what: "補成果", how: "提供驗證方式", need_from_user: "成效"}],
  rewrites: [{original: "維護 API", suggested: "維護 API〔待補：成效〕〔待補:人數〕", reason: "更具體"}],
  angles: [{angle: "jd_match", name: "JD 關鍵條件對照", status: "needs", reason: "缺成效"},
    {angle: "manager", name: "用人主管", status: "meets", reason: "有工作內容"}],
  removed_rewrites: 2, quote_check: "unchecked_image",
};

const expected = `【履歷修改清單】
目標：後端工程師
類型：使用者提供的職缺內容
產生日期：2026/10/25

分組：優先修改重點
同一模型的六種檢查角度，分組只用來排修改優先順序

【優先修改項】
1. 改什麼：補成果
   怎麼改：提供驗證方式
   需要你提供：成效

【逐句改寫】
1. 原句：維護 API
   建議句：維護 API〔待補：成效〕〔待補:人數〕
   原因：更具體

【待補清單】
1. 成效
2. 人數

【六個角度】
1. 職缺條件對照：需補強
   理由：缺成效
2. 用人主管：已具備
   理由：有工作內容

【註記】
1. 已移除 2 組無法核對的建議
2. 原句未逐字核對
3. 數字與經歷請以實際為準
`;
const before = JSON.stringify(data);
const txt = resumeReviewChecklist(data, "txt", date);
assert.equal(txt.content, "\ufeff" + expected, "text content follows the complete ordered contract");
assert.equal(txt.filename, "履歷修改清單-後端工程師-20261025.txt");
assert.equal(txt.type, "text/plain;charset=utf-8");
assert.deepEqual([...Buffer.from(txt.content).subarray(0, 3)], [239, 187, 191], "UTF-8 BOM bytes");
assert.doesNotMatch(txt.content, /[#*`-]/, "plain text formatter adds no Markdown symbols");
assert.doesNotMatch(txt.content, /JD|不應匯出/, "no old display label or debate data");
const md = resumeReviewChecklist(data, "md", date);
assert.equal(md.content, expected.replace(/【([^】]+)】/g, (_, title) => `${title === "履歷修改清單" ? "#" : "##"} ${title}`)
  .replace(/^(?!#)(.+)$/gm, "$1  "));
assert.equal(md.filename, "履歷修改清單-後端工程師-20261025.md");
assert.equal(md.type, "text/markdown;charset=utf-8");
assert.notEqual(md.content.charCodeAt(0), 0xfeff);
assert.equal(JSON.stringify(data), before, "export must not mutate the saved event");

for (const extension of ["txt", "md"]) {
  // A stale group must never leak into a direction export.
  const direction = resumeReviewChecklist({...data, target: {type: "direction", label: "後端"}, has_jd: false}, extension, date);
  assert.ok(direction.content.includes("方向檢查，未給分組"));
  assert.doesNotMatch(direction.content, /分組：|優先修改重點/);
  const noVerdict = resumeReviewChecklist({...data, has_jd: false}, extension, date);
  assert.doesNotMatch(noVerdict.content, /分組：|優先修改重點/);
  const empty = resumeReviewChecklist({target: {type: "job", label: "職缺"}, has_jd: true, group: "可開始投遞，持續微調"}, extension, date);
  assert.ok(empty.content.includes("類型：職缺"));
  assert.ok(empty.content.includes("已移除 0 組無法核對的建議"));
  assert.doesNotMatch(empty.content, /undefined|null|原句未逐字核對/);
}

for (const [label, cleaned] of [[' A<>:"/\\|?*\x00\x1f\x7fB. ', "AB"], [" . ", "未命名"], ["", "未命名"], ["工程師".repeat(50), "工程師".repeat(50).slice(0, 40)], ["😀".repeat(41), "😀".repeat(40)]]) {
  assert.equal(resumeReviewChecklist({...data, target: {type: "direction", label}}, "txt", date).filename,
    `履歷修改清單-${cleaned}-20261025.txt`);
}
assert.equal(resumeReviewChecklist(data, "txt", new Date(2026, 0, 2)).filename, "履歷修改清單-後端工程師-20260102.txt");

const variants = resumeReviewChecklist({...data,
  fixes: [{what: "〔待補： 時程 〕", how: "〔待補:人數〕", need_from_user: " 成效 "}, {what: "補足", how: "方法", need_from_user: "成效"}],
  rewrites: [{original: "C# / part-time", suggested: "〔待補：人數〕〔待補：時程〕〔待補： 〕", reason: "〔待補：證據〕"}],
  angles: [{name: "用人主管", status: "needs", reason: "〔待補：驗證〕"}],
}, "txt", date);
assert.ok(variants.content.includes("原句：C# / part-time"), "keep literal source characters; do not silently corrupt technical skills");
assert.equal(variants.content.split("【待補清單】\n")[1].split("\n【六個角度】")[0], "1. 成效\n2. 時程\n3. 人數\n4. 證據\n5. 驗證\n");
const multiline = {
  ...data,
  fixes: [{what: "補  成果 \n - 項目", how: "提供\n# 標題", need_from_user: "成效\n資料"}],
  rewrites: [{original: "維護  API\n- 項目\n# 標題", suggested: "建議\n內容〔待補： 時程 〕", reason: "更\n具體"}],
  angles: [{name: "用人主管", status: "needs", reason: "缺\n成效〔待補：驗證\n方式〕"}],
};
const multilineBefore = JSON.stringify(multiline);
for (const extension of ["txt", "md"]) {
  const exported = resumeReviewChecklist(multiline, extension, date);
  assert.doesNotMatch(exported.content, /^- /m, "source text must not inject Markdown list lines");
  assert.deepEqual(exported.content.match(/^#+ .*$/gm) || [], extension === "md"
    ? ["# 履歷修改清單", "## 優先修改項", "## 逐句改寫", "## 待補清單", "## 六個角度", "## 註記"] : [],
  "only exporter headings may start with Markdown heading markers");
  for (const field of ["改什麼：補  成果 - 項目", "怎麼改：提供 # 標題", "需要你提供：成效 資料",
    "原句：維護  API - 項目 # 標題", "建議句：建議 內容〔待補： 時程 〕", "原因：更 具體",
    "理由：缺 成效〔待補：驗證 方式〕", "1. 成效 資料", "2. 時程", "3. 驗證 方式"]) {
    assert.ok(exported.content.includes(field), `${extension} preserves field content and collapses newlines: ${field}`);
  }
  if (extension === "md") {
    for (const line of exported.content.split("\n").filter(line => line && !line.startsWith("#"))) {
      assert.ok(line.endsWith("  "), `Markdown content lines need hard breaks: ${line}`);
    }
  } else {
    assert.equal(exported.content.charCodeAt(0), 0xfeff, "multiline text export keeps BOM");
    assert.doesNotMatch(exported.content, / {2}\n/, "text formatter must not add Markdown hard breaks");
  }
}
assert.equal(JSON.stringify(multiline), multilineBefore, "normalizing export fields must not mutate saved values");
console.log("test_resume_checklist: all checks passed");

"""Validate a single-turn resume review and prepare its public card without I/O."""
import re
import unicodedata
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ANGLES = {
    'screening': '人資初篩',
    'jd_match': 'JD 關鍵條件對照',
    'manager': '用人主管',
    'interviewer': '專業面試官',
    'differentiation': '差異化表達',
    'proofreader': '挑剔的旁觀者',
}
GROUPS = ((5, '可開始投遞，持續微調'), (3, '優先修改重點'), (0, '優先補強關鍵內容'))


class ReviewModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class ReviewCase(ReviewModel):
    target_type: Literal['job', 'jd_text', 'direction']
    job_id: str | None = None
    url: str | None = None
    target_label: str = Field(default='', max_length=40)
    positioning: str = Field(max_length=40)


class Issue(ReviewModel):
    issue: str = Field(max_length=30)
    impact: str = Field(max_length=30)


class Defense(ReviewModel):
    responses: list[Annotated[str, Field(max_length=50)]] = Field(max_length=3)
    strengths: list[Annotated[str, Field(max_length=40)]] = Field(max_length=2)


class Angle(ReviewModel):
    angle: Literal[tuple(ANGLES)]
    status: Literal['meets', 'needs']
    reason: str = Field(max_length=50)


class Fix(ReviewModel):
    what: str = Field(max_length=30)
    how: str = Field(max_length=50)
    need_from_user: str = Field(default='', max_length=30)


class Rewrite(ReviewModel):
    original: str = Field(max_length=80)
    suggested: str = Field(max_length=100)
    reason: str = Field(max_length=30)


class ResumeReview(ReviewModel):
    resume_attachment: int
    case: ReviewCase
    prosecution: list[Issue] = Field(max_length=3)
    defense: Defense
    angles: list[Angle] = Field(min_length=6, max_length=6)
    fixes: list[Fix] = Field(max_length=3)
    rewrites: list[Rewrite] = Field(max_length=5)


RULES = ('不新增履歷沒有的經歷、技能、成果、職稱或數字；需要缺少的數字時用〔待補：…〕佔位，'
         '並在對應修改項 need_from_user 說明。不得引用市場統計或錄取率，扣分點前不寫正面評語。'
         'original 必須直接逐字抄錄附件解析文字，不先修正錯字或斷行；只改寫工作內容，不引用或改寫姓名、電話、email 等聯絡資料。'
         '分組只能引用工具回傳值，無分組時不得自行下判決。每個角度獨立依履歷、JD 與檢辯內容判斷，不參照其他角度。')
DESCRIPTION = ('使用者附履歷並要求檢查、評估或優化時，確定一個目標後提交；成功只限一次，失敗可修正一次，stopped=true 時以文字說明未完成並停止呼叫。'
               'resume_attachment 從 1 起算，單一附件填 1（標籤為「本次附件」）；多附件依本次第幾個附件選擇履歷。'
               'case.target_type=job：job_id 或 url 恰給一個，先在本回合 get_job_details 取得 success 且 summary 非空的 JD；讀不到請使用者貼 JD，取得同意才改方向。'
               'jd_text：使用者在訊息或附件提供 JD；direction：使用者指定或同意的職務方向，不出分組。後兩者不得提供 job_id 或 url。'
               '沒有附件請先要求附上；沒有目標或方向先問，不提交。不得送判決、分組或計數欄位。六角度各一：'
               + '、'.join(f'{code}（{name}）' for code, name in ANGLES.items()) + '。' + RULES)


def failure(code, details=None):
    return {'accepted': False, 'error': code, 'details': details or [], 'stopped': False}


def field_path(loc):
    path = ''
    for part in loc:
        path += f'[{part}]' if isinstance(part, int) else ('.' if path else '') + str(part)
    return path


def normalize(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))


def checked_rewrites(rewrites, attachment):
    if attachment.get('kind') == 'image':
        return [item.model_dump() for item in rewrites], 0, 'unchecked_image'
    source = normalize(attachment.get('text', ''))
    kept = []
    for item in rewrites:
        original = normalize(item.original)
        before = set(re.findall(r'\d+', unicodedata.normalize('NFKC', item.original)))
        suggested = re.sub(r'〔待補[:：][^〕]*〕', '', unicodedata.normalize('NFKC', item.suggested))
        after = set(re.findall(r'\d+', suggested))
        if original and original in source and after <= before:
            kept.append(item.model_dump())
    return kept, len(rewrites) - len(kept), 'checked'


def submit_review(arguments, contexts, job_reads, allowed_job_ids):
    try:
        review = ResumeReview.model_validate(arguments)
    except ValidationError as exc:
        # Never echo pydantic's input/msg/ctx: they can contain resume text.
        codes = {'string_too_long': 'too_long', 'extra_forbidden': 'extra_field'}
        # Extra-field names are arbitrary model input; only schema-known paths are returned.
        details = [{'path': field_path(error['loc'][:-1] if error['type'] == 'extra_forbidden' else error['loc']),
                    'code': codes.get(error['type'], error['type'])} for error in exc.errors()]
        return failure('invalid_structure', details), None
    problems = []
    if len(review.defense.responses) != len(review.prosecution):
        problems.append({'path': 'defense.responses', 'code': 'count_mismatch'})
    if {item.angle for item in review.angles} != set(ANGLES):
        problems.append({'path': 'angles', 'code': 'duplicate_angle'})
    case = review.case
    given = [v for v in (case.job_id, case.url) if v is not None]
    if (case.target_type == 'job' and (len(given) != 1 or not given[0].strip())
            or case.target_type != 'job' and given):
        problems.append({'path': 'case', 'code': 'invalid_target'})
    if case.target_type != 'job' and not case.target_label.strip():
        problems.append({'path': 'case.target_label', 'code': 'missing'})
    if problems:
        return failure('invalid_structure', problems), None
    if not 1 <= review.resume_attachment <= len(contexts):
        return failure('attachment_not_found'), None
    target = {'type': case.target_type, 'label': case.target_label}
    if case.target_type == 'job':
        if case.job_id:
            if case.job_id not in allowed_job_ids:
                return failure('target_not_found'), None
            reads = [read for read in job_reads if read.get('job_id') == case.job_id]
            if not reads:
                return failure('job_not_read'), None
        else:
            reads = [read for read in job_reads if read.get('job_id') is None and read.get('url') == case.url]
            if not reads:
                return failure('target_not_found'), None
        usable = [read for read in reads if read.get('status') == 'success'
                  and ((read.get('detail') or {}).get('summary') or '').strip()]
        if not usable:
            return failure('jd_unavailable'), None
        read = usable[-1]
        item, detail = read.get('item') or {}, read.get('detail') or {}
        target.update(label=item.get('title') or detail.get('title') or '職缺',
                      company_name=item.get('company_name') or detail.get('company_name') or '',
                      url=item.get('url') or read.get('url') or '',
                      identity_verified=bool(read.get('identity_verified')))
    has_jd = case.target_type != 'direction'
    meets = sum(item.status == 'meets' for item in review.angles)
    group = next(name for minimum, name in GROUPS if meets >= minimum) if has_jd else None
    by_angle = {item.angle: item for item in review.angles}
    rewrites, removed, quote_check = checked_rewrites(review.rewrites, contexts[review.resume_attachment - 1])
    card = {'target': target, 'has_jd': has_jd, 'group': group, 'positioning': case.positioning,
            'prosecution': [item.model_dump() for item in review.prosecution], 'defense': review.defense.model_dump(),
            'angles': [by_angle[code].model_dump() | {'name': name} for code, name in ANGLES.items()],
            'fixes': [item.model_dump() for item in review.fixes], 'rewrites': rewrites,
            'removed_rewrites': removed, 'quote_check': quote_check}
    result = {'accepted': True, 'has_jd': has_jd, 'group': group, 'removed_rewrites': removed,
              'quote_check': quote_check, 'needs': [code for code in ANGLES if by_angle[code].status == 'needs']}
    return result, card

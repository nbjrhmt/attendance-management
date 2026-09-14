"""AI 知识库种子脚本：写入平台使用指南 Q&A（轻量 RAG 语料）。

用法（在项目根目录执行）::

    # 1) 先确认 .env 中的数据库配置正确
    python -c "from src.common.database import ping_database; print(ping_database())"

    # 2) 写入知识库（幂等：已存在的条目跳过，可重复执行）
    python scripts/seed_ai_knowledge.py

    # 3) 重灌：先清空知识库再写入（内容有更新时使用）
    python scripts/seed_ai_knowledge.py --force

    # 4) 只看会写入什么，不落库
    python scripts/seed_ai_knowledge.py --dry-run

说明：

- 内容提炼自 ``README.md`` 的常见问题（FAQ）与 ``docs/api.md`` 的业务规则，
  覆盖注册建档、成员管理、人脸录入与照片、签到方式与时间窗、迟到判定、缺勤生成、
  请假流程、成员删除语义、统计口径、限流与登录账号类型等；
- 脚本会先执行 ``init_db()``，因此首次部署时无需手动建表；
- ``keywords`` 为逗号分隔的检索词，:func:`src.assistant.service.search_knowledge`
  按"命中关键词个数"打分排序（轻量 RAG，无需向量库）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.common.database import SessionLocal, init_db, ping_database  # noqa: E402
from src.assistant import crud as assistant_crud  # noqa: E402

#: 知识库条目：(标准问题, 检索关键词, 标准答案)
KNOWLEDGE_ITEMS: list[tuple[str, str, str]] = [
    (
        "家庭用户如何注册账号？",
        "注册,户主,开通账号,register,新用户",
        "家庭用户（户主）通过 POST /api/auth/register 自行注册，角色固定为 family；"
        "注册成功后系统会在同一事务内自动创建家庭档案与户主成员记录，无需管理员介入。"
        "注册请求体示例：{\"username\":\"zhangsan\",\"password\":\"zhangsan123456\","
        "\"real_name\":\"张三\"}；密码长度 6~32 位。管理员账号不能用注册接口创建，"
        "需执行 scripts/create_admin.py。",
    ),
    (
        "如何添加或修改家庭成员？",
        "添加成员,新增成员,修改成员,members,家属",
        "户主通过 POST /api/members 添加成员（管理员需额外指定 family_id），"
        "可填写身份证号自动识别性别与出生日期；修改用 PUT /api/members/{member_id}，"
        "启用/停用用 PUT /api/members/{member_id}/status。"
        "每个成员可单独设置 needs_checkin（是否需要签到），设为 false 的成员不参与签到与缺勤生成。",
    ),
    (
        "如何删除家庭成员？删除后还能查到吗？",
        "删除成员,移除成员,停用成员,delete member",
        "阶段五起「删除成员」（DELETE /api/members/{member_id}）的语义是**停用**"
        "（status=inactive），接口仍返回 200「删除成功」，但成员会保留在列表与详情中"
        "（状态为已停用），不再参与签到与缺勤生成，重复删除返回 409。"
        "原因是签到记录与请假记录都以成员为外键，物理删除会破坏历史统计数据；"
        "人脸记录同样保留，人脸识别时会自动过滤已停用成员。",
    ),
    (
        "如何为成员录入人脸？支持哪些照片格式？",
        "人脸录入,录入人脸,face register,照片格式,上传照片",
        "户主本人或管理员调用 POST /api/face/register（multipart：member_id + file）录入；"
        "重新录入用 POST /api/face/update，删除人脸用 POST /api/face/delete。"
        "照片支持 JPG/PNG/BMP，单张默认不超过 2MB（FACE_MAX_IMAGE_MB 可配置）；"
        "默认录入前会先做人脸检测（FACE_DETECT_ON_REGISTER=true），"
        "照片无人脸返回 400「照片中未检测到人脸」，质量不合格返回 400。",
    ),
    (
        "人脸识别报 503 或未配置密钥怎么办？",
        "503,人脸识别失败,百度密钥,local模式,未配置密钥",
        "人脸识别默认使用百度 AI 人脸库（FACE_PROVIDER=baidu），"
        "需在 .env 配置 BAIDU_FACE_API_KEY / BAIDU_FACE_SECRET_KEY；未配置时接口返回 503。"
        "若暂未申请密钥、只想联调流程，可设置 FACE_PROVIDER=local："
        "照片与录入状态照常保存，但**不做人脸比对**，搜索恒定返回「未识别」，"
        "生产环境（ENV=production）会拒绝启用 local 模式。",
    ),
    (
        "人脸照片保存在哪里？可以直接用 URL 访问吗？",
        "照片存储,照片路径,uploads,人脸照片,photo",
        "照片保存在 uploads/face/{家庭ID}/ 目录下，文件名形如 "
        "{成员ID}_{时间戳}_{MD5前8位}.jpg（FACE_UPLOAD_DIR 可配置，已加入 .gitignore）。"
        "uploads/ **不会作为静态目录对外暴露**（那是全村的人脸数据），"
        "读取必须走带鉴权的 GET /api/face/photo/{member_id} 接口；"
        "删除人脸只把数据库记录置为 inactive，照片文件保留在磁盘上由运维按需清理。",
    ),
    (
        "平台支持哪些签到方式？",
        "签到方式,人脸签到,手动签到,checkin,怎么签到",
        "两种方式：① 人脸识别签到 POST /api/checkins/face（multipart：event_id + 现场照片，"
        "复用 1:N 人脸识别，识别到成员后落库并记录识别得分），"
        "由管理员或工作人员操作；② 手动签到 POST /api/checkins/manual"
        "（event_id + member_id，人脸不可用时的备用方式）。"
        "签到必须由管理员/工作人员发起，家庭用户不能自行签到。",
    ),
    (
        "签到的时间窗口是怎么规定的？",
        "签到窗口,活动未开始,活动已结束,签到时间,时间窗",
        "签到必须在活动时间窗内：start_time ≤ 当前时间 ≤ end_time，"
        "且活动状态为 pending 或 active。未开始返回 400「活动尚未开始」，"
        "超过 end_time 或活动已结束返回 400「活动已结束」，活动已取消返回 400「活动已取消」。"
        "如时间确实不合理，请管理员用 PUT /api/events/{event_id} 调整起止时间。",
    ),
    (
        "迟到是怎么判定的？",
        "迟到,迟到判定,late,阈值,迟到阈值",
        "迟到阈值由活动的 late_threshold_minutes 决定（创建活动时指定，默认 15 分钟），"
        "判定规则：checked_at − event.start_time > 阈值 × 60 秒即为 late，否则为 signed。"
        "例如 09:00 开始、阈值 15 分钟，09:16 签到记为迟到。"
        "阈值可在 PUT /api/events/{event_id} 中修改，取值范围 0~1440 分钟。",
    ),
    (
        "活动结束后为什么多出了很多缺勤记录？",
        "缺勤,absent,活动结束,缺勤生成,自动生成",
        "活动状态迁移到 finished 时，系统会在同一次事务内为**所有应签到但无签到记录**的成员"
        "生成记录：已通过请假的成员记为 leave（请假），其余记为 absent（缺勤）；"
        "自动生成的记录 method 与 checked_at 均为 NULL，备注为「活动结束时自动生成」。"
        "「应签到成员」= 家庭正常 + 成员正常 + needs_checkin=true。",
    ),
    (
        "缺勤记录可以人工修正吗？",
        "修正签到,人工修正,异常,abnormal,改签到",
        "可以，管理员调用 PUT /api/checkins/{checkin_id} 修正签到状态"
        "（signed/late/absent/leave/abnormal），同时记录修正人 reviewed_by_id、"
        "修正时间 reviewed_at 与修正说明 review_remark。"
        "修正为 signed/late 时若缺少签到时间则以修正时间补齐、缺少签到方式则补为 manual；"
        "修正为 absent/leave/abnormal 时会清空签到时间与签到方式。",
    ),
    (
        "请假流程是怎样的？谁可以审批？",
        "请假,请假流程,审批,leave,approve,提交请假",
        "户主为本户成员提交：POST /api/leaves（event_id + member_id + reason），"
        "管理员可为任意成员提交；管理员通过 PUT /api/leaves/{leave_id}/approve 审批"
        "（approved 通过 / rejected 驳回）；户主本人或管理员可通过 "
        "PUT /api/leaves/{leave_id}/cancel 撤销**待审批**的申请。"
        "同一成员同一活动同时只能有一条「待审批/已通过」的请假，重复提交返回 409；"
        "已通过请假只在活动结束时影响缺勤生成，若该成员实际到场签到则以实际签到记录为准。",
    ),
    (
        "如何查看本户的签到情况？",
        "本户签到,查看签到,签到明细,家庭签到,我家签到",
        "调用 GET /api/checkins/events/{event_id} 可查看活动签到明细与汇总："
        "summary 包含应签到人数、各状态计数与出勤率，checkins 为分页明细。"
        "家庭用户只会看到**本户**的明细与本户视角的汇总（避免泄露全村村民出勤情况），"
        "管理员与工作人员看到全村数据；也可以在 AI 助手里直接问「我家签到情况」。",
    ),
    (
        "出勤率是怎么算的？",
        "出勤率,统计口径,attendance_rate,怎么算,平均出勤率",
        "出勤率 = (signed + late) / 应签到人数，保留 4 位小数；"
        "应签到人数 = 家庭正常 + 成员正常 + needs_checkin=true 的成员总数。"
        "单活动口径见 GET /api/checkins/events/{event_id} 的 summary；"
        "总览的平均出勤率是各**已结束活动**出勤率的算术平均（2 位小数）；"
        "家庭排行维度分母取该户在已结束活动中的签到记录总数（历史口径）。",
    ),
    (
        "统计报表有哪些？家庭用户能看吗？",
        "统计报表,statistics,总览,家庭排行,趋势,导出",
        "共有五个报表：GET /api/statistics/overview（全村总览）、"
        "/events/{id}（单活动统计）、/families（家庭参与度排行，仅参与过已结束活动的家庭入榜）、"
        "/trend（签到趋势，默认最近 30 天）、/export（CSV 导出，UTF-8 BOM，Excel 不乱码）。"
        "报表含全村数据，**仅管理员与工作人员可访问**，家庭用户返回 403；"
        "家庭用户请使用本户视角接口（GET /api/checkins/events/{id}、GET /api/families/me）。",
    ),
    (
        "活动状态是怎么流转的？",
        "活动状态,状态机,开始活动,结束活动,取消活动",
        "状态机：pending（未开始）→「开始」→ active（进行中）→「结束」→ finished（已结束），"
        "或 pending →「取消」→ cancelled（已取消）；通过 PUT /api/events/{event_id}/status 迁移。"
        "非法迁移返回 409（如 active → cancelled、finished → active、同状态重复迁移）。"
        "只有 pending（未开始）且没有签到/请假记录的活动才能被删除。",
    ),
    (
        "接口调用频率有限制吗？报 429 怎么办？",
        "限流,QPS,429,请求过于频繁,重试",
        "平台返回码 429 表示「请求过于频繁」（触发限流），请降低调用频率后重试。"
        "百度人脸识别免费版约 2 QPS，连续调用可能返回「Open api qps request limit reached」"
        "并被平台映射为 503，建议两次人脸接口调用之间间隔 1 秒以上，"
        "遇到限流等待 2 秒后重试（集成测试中的 face_upload/face_json 即按此策略实现）。",
    ),
    (
        "登录后令牌有效期多久？过期了怎么办？",
        "登录,令牌,access_token,refresh_token,过期,有效期",
        "POST /api/auth/login 返回 access_token（默认 120 分钟）与 refresh_token（默认 7 天）。"
        "access_token 过期后接口返回 401「未认证或认证已失效」，"
        "此时用 POST /api/auth/refresh 携带 refresh_token 换取新令牌（滑动续期），"
        "无需重新输入密码；有效期由 .env 的 ACCESS_TOKEN_EXPIRE_MINUTES 配置。",
    ),
    (
        "平台有哪些账号类型？各自能做什么？",
        "角色,账号类型,admin,staff,family,权限",
        "三种角色：admin（管理员）可创建签到活动、管理全部数据、查看统计报表、审批请假、"
        "生成活动简报；staff（工作人员）可协助现场签到（人脸/手动）、查看名单与统计报表，"
        "但不能修改家庭信息、不能审批请假、不能生成简报；"
        "family（家庭用户/户主）可注册建档、管理本户成员、录入人脸、提交请假，"
        "并只能查看本户的签到与请假数据。账号类型无法自行切换，需要管理员调整。",
    ),
    (
        "AI 助手能做什么？会编造数据吗？",
        "AI助手,智能助手,assistant,对话,工具调用",
        "AI 助手可以查活动、查本户/全村签到汇总、查请假、查统计报表、检索使用指南，"
        "还能一键生成活动简报（POST /api/assistant/events/{id}/summary）。"
        "它通过工具调用复用平台接口的权限链，所有数字都来自平台真实数据，"
        "系统提示词中明确要求「先查数据再回答、不得编造数字」；"
        "默认 LLM_PROVIDER=mock 无需密钥即可体验完整流程。",
    ),
]


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="写入 AI 知识库（平台使用指南 Q&A），幂等可重复执行",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="先清空知识库再写入（内容更新时使用）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将要写入的条目，不落库",
    )
    return parser.parse_args()


def main() -> int:
    """脚本入口。

    :return: 进程退出码，0 表示成功
    """
    args = parse_args()

    print(f"待写入知识库条目：{len(KNOWLEDGE_ITEMS)} 条")
    if args.dry_run:
        for index, (question, keywords, _answer) in enumerate(KNOWLEDGE_ITEMS, start=1):
            print(f"  {index:2d}. {question}（关键词：{keywords}）")
        print("[dry-run] 未连接数据库，未写入任何数据")
        return 0

    if not ping_database():
        print(
            "[错误] 无法连接数据库，请检查 .env 中的 "
            "DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME"
        )
        return 1

    print("[1/3] 数据库连接正常，正在确保数据表存在 ...")
    init_db()

    with SessionLocal() as session:
        if args.force:
            removed = assistant_crud.delete_all_knowledge(session)
            print(f"[2/3] --force：已清空知识库（删除 {removed} 条）")
        else:
            print("[2/3] 幂等模式：已存在的条目将跳过")

        created = 0
        skipped = 0
        for question, keywords, answer in KNOWLEDGE_ITEMS:
            if assistant_crud.get_knowledge_by_question(session, question) is not None:
                skipped += 1
                continue
            assistant_crud.create_knowledge(
                session, question=question, keywords=keywords, answer=answer
            )
            created += 1

        total = assistant_crud.count_knowledge(session)

    print(f"[3/3] 完成：新增 {created} 条，跳过 {skipped} 条，知识库现有 {total} 条")
    print("\n现在可以在 AI 助手中提问「如何添加家庭成员」「人脸照片存在哪里」等使用类问题。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

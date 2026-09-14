"""人脸模块：人脸录入、人脸识别（1:N 搜索）、照片管理与成员人脸状态。

已实现（阶段四）：

- ``POST /api/face/register``：录入人脸（multipart 上传照片）—— 户主本人/管理员
- ``POST /api/face/update``：重新录入（更新）人脸 —— 户主本人/管理员
- ``POST /api/face/delete``：删除人脸 —— 户主本人/管理员
- ``POST /api/face/search``：人脸搜索（1:N 识别）—— 管理员/工作人员
- ``GET  /api/face/status/{member_id}``：成员人脸录入状态
- ``GET  /api/face/records``：人脸录入记录列表（分页/筛选）
- ``GET  /api/face/photo/{member_id}``：读取人脸照片（图片流，需鉴权）

模块结构：

- ``models``：``face_record`` 表（与家庭成员一对一）与提供方/状态枚举
- ``schemas``：请求/响应数据结构
- ``storage``：照片格式（魔数）与大小校验、落盘、路径解析
- ``baidu_client``：百度 AI 人脸识别 V3 HTTP 客户端（token 缓存、错误码映射）
- ``provider``：人脸识别提供方抽象（百度实现 / 本地模式）与依赖工厂
- ``crud``：数据访问层
- ``service``：业务逻辑、权限规则、照片与提供方编排
- ``router``：接口层

配置项见 ``.env.example`` 的人脸识别分组：``FACE_PROVIDER``、``BAIDU_FACE_API_KEY``、
``BAIDU_FACE_SECRET_KEY``、``BAIDU_FACE_GROUP_ID``、``FACE_MATCH_THRESHOLD``、
``FACE_UPLOAD_DIR``、``FACE_MAX_IMAGE_MB`` 等。

未接入活体检测：识别"是否为真人本人"属于签到场景需求，将在阶段五的签到流程中按需调用。
"""

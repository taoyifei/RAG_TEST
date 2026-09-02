# DOCX OOXML v4 支持矩阵

`docx-ooxml-v4` 直接读取 OOXML package，输出 Document IR V1。下表中的状态只描述
Parser 表示能力，不代表 Word 排版还原、OCR 或检索质量。

| 能力 | 状态 | 合同边界 |
|---|---|---|
| 主正文段落与原始 block 顺序 | full | 支持段落、表格、普通内容控件、customXml 与 final-view 修订 wrapper。 |
| Heading 1—9、中文标题、outlineLvl | full | 按可证明样式属性识别，不根据字号或粗体猜测。 |
| 自定义标题样式 | full | 仅接受 `ParsingPolicy.custom_heading_styles` 的显式映射。 |
| 样式继承与环检测 | full | 解析 basedOn、next、link、qFormat、隐藏属性；缺失父级和环生成 issue。 |
| Run 文本、Tab、换行与连字符 | full | 同 run 或跨 run 不自动补空格；page/column break 形成 BreakNode。 |
| 字段 | partial | simple/complex 字段使用已有 result；指令只保存类型与摘要，不执行字段。 |
| 超链接与书签 | partial | 显示文字与内部目标保留；外链只保存协议类型，不保存或访问 URL。 |
| 自动编号与多级列表 | full | 支持独立 numId、startOverride、lvlRestart、常见数字/字母/罗马/中文格式。 |
| 未知 numFmt 与图片项目符号 | metadata | 保留 numId、level 并生成 issue，不猜显示标签或渲染图片项目符号。 |
| 表格物理结构与逻辑 grid | full | 保留 Table/Row/Cell、gridSpan、vMerge、gridBefore/After、嵌套表格与原始子节点顺序。 |
| 无效表格网格 | reject | strict 拒绝；best_effort 保留物理 cell 并生成 issue。 |
| Section 与页面设置摘要 | full | 保存 break、尺寸、边距、titlePg 与 story binding，不伪造页码。 |
| 页眉页脚与跨 Section 继承 | full | Part 只解析一次，effective binding 明确记录 inherited_from。 |
| 脚注与尾注 | full | 独立 story/node 与正文关系；不把 note 全文拼入正文段落。 |
| 批注 | partial | 默认 metadata-only；include 时形成独立 story，并保存 range/reference ID、作者和时间字段。 |
| Tracked changes final view | full | 保留 ins/moveTo，排除 del/moveFrom，并输出 RevisionMark 与计数。 |
| Tracked changes all_with_markers | metadata | 删除文字只计数字符与修订 metadata，不进入默认检索正文。 |
| 图片 inline/anchor/VML | full | 每个显示实例独立节点，相同 media 共享 Blob；不做解码或 OCR。 |
| 图片 caption 与复杂浮动位置 | partial | 保存安全尺寸、placement 和 alt/title；不还原完整 Word 布局。 |
| 外链图片与外部关系 | metadata | 只保存 scheme/关系类型，绝不下载；策略可直接拒绝。 |
| Text Box | full | 作为 `TEXT_BOX` story 递归解析段落、表格和图片。 |
| Office Math、OLE、SmartArt | metadata | 保存可见 fallback 或 issue；不执行、不解包、不渲染。 |
| altChunk 与含证据的未知 wrapper | reject | strict 拒绝；best_effort 形成 UnsupportedNode 并降低 coverage。 |
| 宏、加密条目、路径逃逸、资源超限 | reject | 两种解析模式都以 fatal `InvalidDocument` 失败。 |

旧索引不会自动切换 Parser。开发离线 Profile 使用 v4，旧生产 Profile 可继续显式选择
`legacy-docx-ir`；迁移到 v4 会改变 parser fingerprint，必须创建新的 index revision。

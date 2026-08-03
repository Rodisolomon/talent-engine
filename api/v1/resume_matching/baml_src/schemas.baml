// parse.baml 与 match.baml 共用的数据结构。
//
// 候选人与岗位的字段几乎都可为空，避免脏输入逼着模型编造内容。
// Job.raw_text 保留原始段落，便于匹配时在 gaps 里引用原文。

class Resume {
  name string?
  gender string?                    // "男" / "女" / null
  birth_year int?                   // 用于推算年龄
  age int?                          // 简历直接写明时才填

  phone string?
  email string?
  hometown string?

  // 教育经历，最新的排在最前。当前学校 + 专业 + 学历通常是第一条。
  education Education[]

  // 工作 / 实习 / 校园经历 —— 任何有报酬或有组织的经历
  experience Experience[]

  certifications string[]           // 证书: "护士执业资格证", "C1驾照", "普通话二级甲等"
  skills string[]                   // 技能: "Python", "Office", "ERP系统"
  languages string[]                // "英语", "日语"
  self_evaluation string?           // 自我评价原文
  raw_text string                   // 简历全文，供匹配时补充上下文
}

class Education {
  school string
  degree string?                    // "高中", "中专", "专科", "本科", "研究生"
  major string?
  start string?                     // "2022.9" 或 "2022-09"
  end string?                       // "2026.6"
  gpa_or_rank string?               // 例如 "专业前10%"
}

class Experience {
  organization string
  title string
  start string?
  end string?
  description string                // 合并后的要点
}

// ---------------------------------------------------------------------------

class Job {
  company string                    // 招聘单位
  position string                   // 招聘岗位

  // 硬性条件 —— 大多可为空，模型只提取 JD 明确写出的部分
  education_min string?             // "中专", "专科", "本科" (null = 不限)
  age_min int?
  age_max int?
  majors_preferred string[]         // 专业倾向 ("空乘", "计算机相关", "金融/经济")
  experience_years_min int?
  gender_preference string?         // "不限", "男", "女"
  height_min_cm int?
  certifications_required string[]  // 护士证, 驾照
  image_requirements string?        // 形象气质原文（主观，不参与评分）

  duties string[]                   // 工作内容要点
  salary_min int?                   // 月薪 RMB
  salary_max int?
  work_schedule string?             // "长白班 / 双休 / 上12休36"
  location string                   // 工作地点（城市 + 区）
  benefits string[]                 // "五险一金", "双休", "食宿"
  raw_text string                   // JD 原文全文
}

// ---------------------------------------------------------------------------

// 一次「简历 × 岗位」的匹配结果。
//
// 模型只给五个分项，总分和 verdict 由 Python 算 —— 不让模型做算术：实测
// deepseek-chat 在 8 次可核对的运行里有 7 次报出的总分与它自己写的分项对
// 不上，且总是偏低 6-22 分。权重要真正落地，加法就不能交给模型。
// 顺带的好处：权重和档位阈值变成 Python 常量，调整时不用改 prompt。
class MatchScore {
  score_experience int              // 工作经历 0-40
  score_skills int                  // 专业技能 0-20
  score_education int               // 学历 0-20
  score_age int                     // 年龄 0-10
  score_other int                   // 其他（工作地点 + 优先条件）0-10

  hard_fails string[]               // 缺少的必需证书；无则为空
  strengths string[]                // 简历与岗位的具体契合点
  gaps string[]                     // 候选人缺什么（简历指导服务的抓手）
  reasoning string                  // 一到两句结论
}

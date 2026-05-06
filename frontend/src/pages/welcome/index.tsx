import { Button, Card, Col, Row, Space, Tag, Typography } from 'antd'
import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { useSnapshot } from 'valtio'
import { userState } from '@/store/user'
import styles from './index.module.scss'

const { Title, Paragraph, Text } = Typography

interface DimensionCard {
  key: string
  name: string
  question: string
  exampleScore: string
  exampleLevel: 'high' | 'medium' | 'low' | 'clean'
}

const DIMENSIONS: DimensionCard[] = [
  {
    key: 'opening_hook',
    name: '开场钩子',
    question: '前 3 集前 3 场是否抓人？',
    exampleScore: '8/10',
    exampleLevel: 'high',
  },
  {
    key: 'reward_density',
    name: '爽点密度',
    question: '反转 / 打脸 / 逆袭密度够不够？',
    exampleScore: '6/10',
    exampleLevel: 'medium',
  },
  {
    key: 'motivation',
    name: '动机自洽',
    question: '关键决策有没有铺垫？',
    exampleScore: '5/10',
    exampleLevel: 'medium',
  },
  {
    key: 'pacing',
    name: '节奏控制',
    question: '中段是否塌陷？',
    exampleScore: '7/10',
    exampleLevel: 'high',
  },
  {
    key: 'risk',
    name: '审核风险',
    question: '有无广电红线 / 题材风险？',
    exampleScore: '9/10',
    exampleLevel: 'clean',
  },
]

const LEVEL_COLOR: Record<DimensionCard['exampleLevel'], string> = {
  high: 'green',
  medium: 'orange',
  low: 'red',
  clean: 'cyan',
}

export default function Welcome() {
  const navigate = useNavigate()
  const user = useSnapshot(userState)

  useEffect(() => {
    // 已登录用户回到 landing 直接进 doc-studio（避免重复展示）
    if (user.token) {
      navigate('/doc-studio', { replace: true })
    }
  }, [user.token, navigate])

  const goLogin = (tab: 'login' | 'register') => {
    const params = new URLSearchParams({ redirect: '/doc-studio', tab })
    navigate(`/login?${params.toString()}`)
  }

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.brand}>
          <span className={styles.brandMark}>SL</span>
          <span className={styles.brandName}>ScriptLens</span>
          <span className={styles.brandSub}>短剧分析助手</span>
        </div>
        <Space>
          <Button type="text" onClick={() => goLogin('login')}>
            已有账号？登录
          </Button>
          <Button type="primary" onClick={() => goLogin('register')}>
            免费注册
          </Button>
        </Space>
      </header>

      <section className={styles.hero}>
        <Title level={1} className={styles.heroTitle}>
          一份长剧本，3 分钟读懂决策点
        </Title>
        <Paragraph className={styles.heroSub}>
          面向短剧 / 微短剧的内容策划、编剧统筹、内容运营、选品与审核 ——
          ScriptLens 是带证据的剧本分析 Agent：上传剧本即可拿到 5 维评分、必读场景、改写建议，
          每一个判断都附带原文场号，可追溯、可改写、可追问。
        </Paragraph>
        <Space size={12} className={styles.heroCta}>
          <Button type="primary" size="large" onClick={() => goLogin('register')}>
            立即体验
          </Button>
          <Button size="large" onClick={() => goLogin('login')}>
            已有账号 · 登录
          </Button>
        </Space>
        <Paragraph className={styles.heroNote} type="secondary">
          支持 .docx / .pdf / .txt / .md ，单文件 ≤ 50MB · 4-6 秒拿到首份诊断报告
        </Paragraph>
      </section>

      <section className={styles.section}>
        <Title level={3}>它解决了什么问题</Title>
        <Row gutter={[24, 24]}>
          <Col xs={24} md={8}>
            <Card variant="borderless" className={styles.painCard}>
              <Tag color="red">读得慢</Tag>
              <Paragraph className={styles.painText}>
                短剧 60-100 集每份动辄 5 万字，读完一份要 2 小时；
                投放 / 选品 / 审核日均要看 5-10 份。
              </Paragraph>
            </Card>
          </Col>
          <Col xs={24} md={8}>
            <Card variant="borderless" className={styles.painCard}>
              <Tag color="orange">摘要不够用</Tag>
              <Paragraph className={styles.painText}>
                AI 摘要只告诉你"讲了什么"，但你真正想知道的是
                "钩子够不够强、中段会不会塌、有没有审核红线"。
              </Paragraph>
            </Card>
          </Col>
          <Col xs={24} md={8}>
            <Card variant="borderless" className={styles.painCard}>
              <Tag color="gold">没有证据</Tag>
              <Paragraph className={styles.painText}>
                LLM 给个分数容易，但说不清"凭什么打这个分"。
                ScriptLens 每条结论都附带 <Text code>scene_id</Text> + 原文片段。
              </Paragraph>
            </Card>
          </Col>
        </Row>
      </section>

      <section className={styles.section}>
        <Title level={3}>5 维评分诊断</Title>
        <Paragraph type="secondary">
          每维参照中文短剧工业判据（抖音 / 快手 / 广电备案）独立打分，
          0-10 + 三档（high / medium / low），证据不足时显式标记
          <Text code>证据不足</Text>，不伪造默认分。
        </Paragraph>
        <Row gutter={[16, 16]}>
          {DIMENSIONS.map((d) => (
            <Col xs={24} sm={12} md={8} lg={Math.floor(24 / 5) || 4} key={d.key}>
              <Card className={styles.dimCard} variant="outlined">
                <div className={styles.dimHeader}>
                  <span className={styles.dimName}>{d.name}</span>
                  <Tag color={LEVEL_COLOR[d.exampleLevel]}>{d.exampleLevel}</Tag>
                </div>
                <div className={styles.dimScore}>{d.exampleScore}</div>
                <Paragraph className={styles.dimQuestion} type="secondary">
                  {d.question}
                </Paragraph>
              </Card>
            </Col>
          ))}
        </Row>
      </section>

      <section className={styles.section}>
        <Title level={3}>4 个核心能力</Title>
        <Row gutter={[24, 24]}>
          <Col xs={24} md={12}>
            <Card variant="outlined" className={styles.capCard}>
              <Title level={5}>结构化诊断</Title>
              <Paragraph type="secondary">
                上传剧本后台自动切集 / 切场，调 LLM 跑 5 维评分，输出
                <Text code>scorecard</Text> + <Text code>decision</Text> +
                <Text code>must_read</Text> 决策卡。
              </Paragraph>
            </Card>
          </Col>
          <Col xs={24} md={12}>
            <Card variant="outlined" className={styles.capCard}>
              <Title level={5}>证据化追问</Title>
              <Paragraph type="secondary">
                在右侧 chat 面板继续追问"为什么 motivation 给 5 分"，Agent 跑
                ReAct 工具链返回流式答案，每条结论可点击跳转原文。
              </Paragraph>
            </Card>
          </Col>
          <Col xs={24} md={12}>
            <Card variant="outlined" className={styles.capCard}>
              <Title level={5}>定向改写</Title>
              <Paragraph type="secondary">
                选中低分场景，按维度（钩子 / 节奏 / 动机）做定向改写，输出
                原文 / 改写版 / unified diff，前端 in-place toggle 比对。
              </Paragraph>
            </Card>
          </Col>
          <Col xs={24} md={12}>
            <Card variant="outlined" className={styles.capCard}>
              <Title level={5}>三视角切换</Title>
              <Paragraph type="secondary">
                同一份报告按 <Text code>选品 / 编剧 / 审核</Text>
                切换重点字段：选品看 ROI、编剧看动机、审核看 risk。
              </Paragraph>
            </Card>
          </Col>
        </Row>
      </section>

      <section className={styles.section}>
        <Title level={3}>使用流程</Title>
        <Row gutter={[16, 16]} align="top">
          <Col xs={24} md={6}>
            <div className={styles.stepCard}>
              <div className={styles.stepIdx}>1</div>
              <Title level={5}>上传剧本</Title>
              <Paragraph type="secondary">
                拖入 .docx / .pdf / .txt / .md
              </Paragraph>
            </div>
          </Col>
          <Col xs={24} md={6}>
            <div className={styles.stepCard}>
              <div className={styles.stepIdx}>2</div>
              <Title level={5}>自动切场</Title>
              <Paragraph type="secondary">
                按集 / 场 / 角色解析，pgvector + BM25 双路索引
              </Paragraph>
            </div>
          </Col>
          <Col xs={24} md={6}>
            <div className={styles.stepCard}>
              <div className={styles.stepIdx}>3</div>
              <Title level={5}>5 维诊断</Title>
              <Paragraph type="secondary">
                ~5 秒生成 scorecard + decision + must_read
              </Paragraph>
            </div>
          </Col>
          <Col xs={24} md={6}>
            <div className={styles.stepCard}>
              <div className={styles.stepIdx}>4</div>
              <Title level={5}>追问 / 改写</Title>
              <Paragraph type="secondary">
                右侧 chat 追问、选区改写、维度反馈进化 skill
              </Paragraph>
            </div>
          </Col>
        </Row>
      </section>

      <section className={styles.ctaSection}>
        <Title level={3}>准备好让 Agent 帮你读剧本了吗？</Title>
        <Space size={12}>
          <Button type="primary" size="large" onClick={() => goLogin('register')}>
            免费注册 · 立即开始
          </Button>
          <Button size="large" onClick={() => goLogin('login')}>
            已有账号 · 登录
          </Button>
        </Space>
      </section>

      <footer className={styles.footer}>
        <Text type="secondary">
          ScriptLens · 复用 ScholarMind Doc Studio Agent 框架（PRD §0.2）·
          所有评分均含 <Text code>scene_id</Text> 证据，可追溯
        </Text>
      </footer>
    </div>
  )
}

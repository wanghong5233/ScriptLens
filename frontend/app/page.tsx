"use client";

import { useState } from "react";

type SourceType = "unknown" | "web_novel" | "screenplay" | "short_drama";

type SampleResponse = {
  title: string;
  text: string;
  source_type: SourceType;
};

type ScriptSegment = {
  id: string;
  script_id: string;
  label: string;
  start_line: number;
  end_line: number;
  text: string;
};

type BasicReport = {
  script_id: string;
  title: string;
  summary: string;
  core_plot: string;
  main_characters: string[];
  key_conflicts: string[];
  hooks: string[];
  risks: string[];
  next_step: string;
  segments: ScriptSegment[];
};

type ScriptDocument = {
  id: string;
  title: string;
  source_type: SourceType;
  raw_text: string;
};

type ScriptCreateResponse = {
  script: ScriptDocument;
  segments: ScriptSegment[];
};

const apiBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export default function Home() {
  const [text, setText] = useState("");
  const [title, setTitle] = useState("小妾");
  const [sourceType, setSourceType] = useState<SourceType>("web_novel");
  const [report, setReport] = useState<BasicReport | null>(null);
  const [status, setStatus] = useState("等待加载示例剧本。");
  const [loading, setLoading] = useState(false);

  async function loadSample() {
    setLoading(true);
    setStatus("正在加载示例剧本...");
    try {
      const response = await fetch(`${apiBaseUrl}/api/sample`);
      if (!response.ok) {
        throw new Error(`Sample API failed: ${response.status}`);
      }
      const sample = (await response.json()) as SampleResponse;
      setText(sample.text);
      setTitle(sample.title);
      setSourceType(sample.source_type);
      setReport(null);
      setStatus("示例剧本已加载,可以开始基础分析。");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "加载示例失败。");
    } finally {
      setLoading(false);
    }
  }

  async function analyze() {
    setLoading(true);
    setStatus("正在创建剧本项目并生成基础报告...");
    try {
      const createResponse = await fetch(`${apiBaseUrl}/api/scripts`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ text, title, source_type: sourceType }),
      });
      if (!createResponse.ok) {
        throw new Error(`Create script API failed: ${createResponse.status}`);
      }
      const created = (await createResponse.json()) as ScriptCreateResponse;
      const analyzeResponse = await fetch(`${apiBaseUrl}/api/scripts/${created.script.id}/analyze`, {
        method: "POST",
      });
      if (!analyzeResponse.ok) {
        throw new Error(`Analyze API failed: ${analyzeResponse.status}`);
      }
      const nextReport = (await analyzeResponse.json()) as BasicReport;
      setReport(nextReport);
      setStatus("基础报告已生成。");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "分析失败。");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="page">
      <section className="hero">
        <h1>ScriptLens</h1>
        <p>上传长剧本,生成可验证的结构化剧本理解报告。当前为前后端分离架构骨架。</p>
      </section>

      <section className="workspace">
        <div className="panel">
          <h2>原文输入</h2>
          <p className="muted">{status}</p>
          <div className="actions">
            <button className="button secondary" onClick={loadSample} disabled={loading}>
              加载示例剧本
            </button>
            <button className="button" onClick={analyze} disabled={loading || text.length < 100}>
              生成基础报告
            </button>
          </div>
          <textarea value={text} onChange={(event) => setText(event.target.value)} />
        </div>

        <div className="panel">
          <h2>基础报告</h2>
          {report ? <ReportView report={report} /> : <p className="muted">报告生成后会显示在这里。</p>}
        </div>
      </section>
    </main>
  );
}

function ReportView({ report }: { report: BasicReport }) {
  return (
    <>
      <section className="report-section">
        <h3>{report.title}</h3>
        <p>{report.summary}</p>
        <p className="segment-count">已识别剧情片段: {report.segments.length}</p>
      </section>

      <section className="report-section">
        <h3>核心主线</h3>
        <p>{report.core_plot}</p>
      </section>

      <section className="report-section">
        <h3>主要人物</h3>
        <ul className="pill-list">
          {report.main_characters.map((character) => (
            <li key={character}>{character}</li>
          ))}
        </ul>
      </section>

      <ListSection title="关键冲突" items={report.key_conflicts} />
      <ListSection title="看点与钩子" items={report.hooks} />
      <ListSection title="问题与风险" items={report.risks} />

      <section className="report-section">
        <h3>下一步</h3>
        <p>{report.next_step}</p>
      </section>
    </>
  );
}

function ListSection({ title, items }: { title: string; items: string[] }) {
  return (
    <section className="report-section">
      <h3>{title}</h3>
      <ul className="bullet-list">
        {items.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </section>
  );
}

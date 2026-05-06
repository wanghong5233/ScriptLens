#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import logging
import json
import re
from service.core.rag.utils.doc_store_conn import MatchTextExpr

from service.core.rag.nlp import rag_tokenizer, term_weight, synonym


class FulltextQueryer:
    def __init__(self):
        """
        初始化全文查询器。

        这个过程会实例化处理词权重和同义词的两个核心组件:
        - term_weight.Dealer: 用于计算词语在查询中的重要性。
        - synonym.Dealer: 用于查找词语的同义词以进行查询扩展。

        同时，它还定义了默认的查询字段列表及其权重，这些字段
        将用于在Elasticsearch中进行多字段搜索。
        """
        self.tw = term_weight.Dealer()
        self.syn = synonym.Dealer()
        self.query_fields = [
            "title_tks^10",
            "title_sm_tks^5",
            "important_kwd^30",
            "important_tks^20",
            "question_tks^20",
            "content_ltks^2",
            "content_sm_ltks",
        ]

    @staticmethod
    def subSpecialChar(line):
        """
        对输入字符串中的Elasticsearch特殊查询字符进行转义。

        Elasticsearch的查询语法中有一些保留字符，如 ':', '/', '*', '\"' 等。
        如果用户的原始查询中包含了这些字符，直接发送会导致查询语法错误。
        此函数的作用是在这些特殊字符前加上反斜杠'\'，使其被当作普通文本处理。

        Args:
            line (str): 待处理的原始字符串。

        Returns:
            str:  经过特殊字符转义后的字符串。
        """
        return re.sub(r"([:\{\}/\[\]\-\*\"\(\)\|\+~\^])", r"\\\1", line).strip()

    @staticmethod
    def isChinese(line):
        """
        判断一个字符串是否主要由中文构成。

        该方法用于区分中英文查询，以便后续采用不同的查询增强策略。
        判断逻辑是：将字符串按空格分割成词，如果非英文字母构成的词
        占总词数的比例达到或超过70%，则认为是中文。

        Args:
            line (str): 待判断的字符串。

        Returns:
            bool: 如果字符串主要为中文，则返回True，否则返回False。
        """
        arr = re.split(r"[ \t]+", line)
        if len(arr) <= 3:
            return True
        e = 0
        for t in arr:
            if not re.match(r"[a-zA-Z]+$", t):
                e += 1
        return e * 1.0 / len(arr) >= 0.7

    @staticmethod
    def rmWWW(txt):
        """
        移除文本中的中英文停用词 (Stop Words)。

        停用词是指在信息检索中，为节省存储空间和提高搜索效率，
        自动过滤掉的某些字或词，这些词通常不携带关键语义信息，
        如 "的"、"是"、"a"、"the" 等。

        Args:
            txt (str): 待处理的原始文本。

        Returns:
            str: 移除了停用词后的文本。
        """
        patts = [
            (
                r"是*(什么样的|哪家|一下|那家|请问|啥样|咋样了|什么时候|何时|何地|何人|是否|是不是|多少|哪里|怎么|哪儿|怎么样|如何|哪些|是啥|啥是|啊|吗|呢|吧|咋|什么|有没有|呀|谁|哪位|哪个)是*",
                "",
            ),
            (r"(^| )(what|who|how|which|where|why)('re|'s)? ", " "),
            (
                r"(^| )('s|'re|is|are|were|was|do|does|did|don't|doesn't|didn't|has|have|be|there|you|me|your|my|mine|just|please|may|i|should|would|wouldn't|will|won't|done|go|for|with|so|the|a|an|by|i'm|it's|he's|she's|they|they're|you're|as|by|on|in|at|up|out|down|of|to|or|and|if) ",
                " ")
        ]
        for r, p in patts:
            txt = re.sub(r, p, txt, flags=re.IGNORECASE)
        return txt

    def question(self, txt, tbl="qa", min_match: float = 0.6):
        """
        将用户的自然语言问题，通过一系列复杂的NLP处理，转换为一个
        高度优化的、用于Elasticsearch全文检索的查询表达式。

        这是RAG系统中将用户意图“翻译”为数据库指令的核心模块。

        处理流程:
        1. 文本预处理: 清洗、标准化、去除停用词。
        2. 查询增强: 进行分词、同义词扩展、细粒度切分，以提高召回率。
        3. 权重分配: 为每个词计算其重要性权重，以提高精准度。
        4. 查询构建: 将处理好的词语和权重，按照ES的查询语法，组装成
           一个复杂的、多字段的查询字符串。

        Args:
            txt (str): 用户的原始自然语言查询。
            tbl (str, optional): 目标表格或索引的标识，此项目中未使用。 Defaults to "qa".
            min_match (float, optional): 查询的最低匹配度要求，用于控制召回的宽松程度。 Defaults to 0.6.

        Returns:
            tuple[MatchTextExpr, list[str]] | tuple[None, list[str]]:
                返回一个元组。
                - 第一个元素是 `MatchTextExpr` 对象，封装了最终的查询逻辑；如果无法生成查询，则为None。
                - 第二个元素是 `keywords` 列表，包含了所有用于查询的关键词，用于结果高亮。
        """
        # 1. 文本预处理：格式化、小写、简体化、移除标点和停用词。
        txt = re.sub(
            r"[ :|\r\n\t,，。？?/`!！&^%%()\[\]{}<>]+",
            " ",
            rag_tokenizer.tradi2simp(rag_tokenizer.strQ2B(txt.lower())),
        ).strip()
        txt = FulltextQueryer.rmWWW(txt)

        # 区分中英文，采用不同的处理逻辑
        if not self.isChinese(txt):
            # --- 英文处理逻辑 ---
            txt = FulltextQueryer.rmWWW(txt)
            tks = rag_tokenizer.tokenize(txt).split()
            keywords = [t for t in tks if t]
            
            # 3. 权重分配：为每个英文token计算权重。
            tks_w = self.tw.weights(tks, preprocess=False)
            tks_w = [(re.sub(r"[ \\\"'^]", "", tk), w) for tk, w in tks_w]
            tks_w = [(re.sub(r"^[a-z0-9]$", "", tk), w) for tk, w in tks_w if tk]
            tks_w = [(re.sub(r"^[\+-]", "", tk), w) for tk, w in tks_w if tk]
            tks_w = [(tk.strip(), w) for tk, w in tks_w if tk.strip()]
            
            # 2. 查询增强：查找同义词并加入关键词列表。
            syns = []
            for tk, w in tks_w:
                syn = self.syn.lookup(tk)
                syn = rag_tokenizer.tokenize(" ".join(syn)).split()
                keywords.extend(syn)
                # 为同义词也构建带权重的查询片段
                syn = ["\"{}\"^{:.4f}".format(s, w / 4.) for s in syn if s.strip()]
                syns.append(" ".join(syn))

            # 4. 查询构建：组装最终的ES查询字符串。
            #    - `(term^weight synonyms)`: 基础查询单元
            #    - `"term1 term2"^weight`: 短语匹配
            q = ["({}^{:.4f}".format(tk, w) + " {})".format(syn) for (tk, w), syn in zip(tks_w, syns) if
                 tk and not re.match(r"[.^+\(\)-]", tk)]
            for i in range(1, len(tks_w)):
                left, right = tks_w[i - 1][0].strip(), tks_w[i][0].strip()
                if not left or not right:
                    continue
                q.append(
                    '"%s %s"^%.4f'
                    % (
                        tks_w[i - 1][0],
                        tks_w[i][0],
                        max(tks_w[i - 1][1], tks_w[i][1]) * 2,
                    )
                )
            if not q:
                q.append(txt)
            query = " ".join(q)
            return MatchTextExpr(
                self.query_fields, query, 100
            ), keywords

        def need_fine_grained_tokenize(tk):
            if len(tk) < 3:
                return False
            if re.match(r"[0-9a-z\.\+#_\*-]+$", tk):
                return False
            return True

        # --- 中文处理逻辑 ---
        txt = FulltextQueryer.rmWWW(txt)
        qs, keywords = [], []
        # 将问题切分为多个独立的查询子句
        for tt in self.tw.split(txt)[:256]:  # .split():
            if not tt:
                continue
            keywords.append(tt)
            
            # 3. 权重分配：为子句中的每个词计算权重。
            twts = self.tw.weights([tt])
            
            # 2. 查询增强：查找子句的同义词。
            syns = self.syn.lookup(tt)
            if syns and len(keywords) < 32:
                keywords.extend(syns)
            logging.debug(json.dumps(twts, ensure_ascii=False))
            tms = []
            
            # 4. 查询构建：对子句中的每个词进行深度处理，构建复杂的查询片段。
            for tk, w in sorted(twts, key=lambda x: x[1] * -1):
                # 2.1 查询增强：进行细粒度分词。
                sm = (
                    rag_tokenizer.fine_grained_tokenize(tk).split()
                    if need_fine_grained_tokenize(tk)
                    else []
                )
                sm = [
                    re.sub(
                        r"[ ,\./;'\[\]\\`~!@#$%\^&\*\(\)=\+_<>\?:\"\{\}\|，。；‘’【】、！￥……（）——《》？：“”-]+",
                        "",
                        m,
                    )
                    for m in sm
                ]
                sm = [FulltextQueryer.subSpecialChar(m) for m in sm if len(m) > 1]
                sm = [m for m in sm if len(m) > 1]

                if len(keywords) < 32:
                    keywords.append(re.sub(r"[ \\\"']+", "", tk))
                    keywords.extend(sm)

                # 2.2 查询增强：查找词的同义词。
                tk_syns = self.syn.lookup(tk)
                tk_syns = [FulltextQueryer.subSpecialChar(s) for s in tk_syns]
                if len(keywords) < 32:
                    keywords.extend([s for s in tk_syns if s])
                tk_syns = [rag_tokenizer.fine_grained_tokenize(s) for s in tk_syns if s]
                tk_syns = [f"\"{s}\"" if s.find(" ") > 0 else s for s in tk_syns]

                if len(keywords) >= 32:
                    break

                # 4.1 查询构建：组合原始词、同义词、细粒度词，形成带权重的查询片段。
                tk = FulltextQueryer.subSpecialChar(tk)
                if tk.find(" ") > 0:
                    tk = '"%s"' % tk
                if tk_syns:
                    tk = f"({tk} OR (%s)^0.2)" % " ".join(tk_syns)
                if sm:
                    tk = f'{tk} OR "%s" OR ("%s"~2)^0.5' % (" ".join(sm), " ".join(sm))
                if tk.strip():
                    tms.append((tk, w))

            # 4.2 查询构建：将所有词的查询片段，根据权重组合成一个子句的完整查询。
            tms = " ".join([f"({t})^{w}" for t, w in tms])

            if len(twts) > 1:
                tms += ' ("%s"~2)^1.5' % rag_tokenizer.tokenize(tt)

            # 4.3 查询构建：将子句自身的同义词作为整体补充查询。
            syns = " OR ".join(
                [
                    '"%s"'
                    % rag_tokenizer.tokenize(FulltextQueryer.subSpecialChar(s))
                    for s in syns
                ]
            )
            if syns and tms:
                tms = f"({tms})^5 OR ({syns})^0.7"

            qs.append(tms)

        if qs:
            # 4.4 查询构建：将所有子句的查询用 OR 连接，形成最终的、完整的查询字符串。
            query = " OR ".join([f"({t})" for t in qs if t])
            return MatchTextExpr(
                self.query_fields, query, 100, {"minimum_should_match": min_match}
            ), keywords
        return None, keywords

    def hybrid_similarity(self, avec, bvecs, atks, btkss, tkweight=0.3, vtweight=0.7):
        """
        计算混合相似度。

        该方法结合了向量余弦相似度和关键词（token）相似度，通过加权求和
        的方式得到一个更全面的混合相似度分数。

        Args:
            avec (list[float]): 查询向量 (A)。
            bvecs (list[list[float]]): 多个文档的向量列表 (B)。
            atks (str | list[str]): 查询的关键词 (A)。
            btkss (list[str | list[str]]): 多个文档的关键词列表 (B)。
            tkweight (float, optional): 关键词相似度的权重. Defaults to 0.3.
            vtweight (float, optional): 向量相似度的权重. Defaults to 0.7.

        Returns:
            tuple[np.ndarray, np.ndarray, np.ndarray]:
                返回一个元组，包含：
                - 混合相似度分数数组
                - 仅关键词相似度分数数组
                - 仅向量相似度分数数组
        """
        from sklearn.metrics.pairwise import cosine_similarity as CosineSimilarity
        import numpy as np

        sims = CosineSimilarity([avec], bvecs)
        tksim = self.token_similarity(atks, btkss)
        return np.array(sims[0]) * vtweight + np.array(tksim) * tkweight, tksim, sims[0]

    def token_similarity(self, atks, btkss):
        """
        计算查询关键词与多个文档关键词之间的相似度。

        Args:
            atks (str | list[str]): 查询的关键词 (A)。
            btkss (list[str | list[str]]): 多个文档的关键词列表 (B)。

        Returns:
            list[float]: 一个列表，包含了查询(A)与每个文档(B)的关键词相似度分数。
        """
        def toDict(tks):
            d = {}
            if isinstance(tks, str):
                tks = tks.split()
            for t, c in self.tw.weights(tks, preprocess=False):
                if t not in d:
                    d[t] = 0
                d[t] += c
            return d

        atks = toDict(atks)
        btkss = [toDict(tks) for tks in btkss]
        return [self.similarity(atks, btks) for btks in btkss]

    def similarity(self, qtwt, dtwt):
        """
        计算两组带权重的关键词集合之间的相似度。

        计算逻辑是：
        (查询词和文档词交集的权重之和) / (查询词的总权重之和)
        这是一种Jaccard相似系数的变体。

        Args:
            qtwt (dict | str): 查询的带权重关键词字典或待处理的字符串。
            dtwt (dict | str): 文档的带权重关键词字典或待处理的字符串。

        Returns:
            float: 两个关键词集合的相似度分数。
        """
        if isinstance(dtwt, type("")):
            dtwt = {t: w for t, w in self.tw.weights(self.tw.split(dtwt), preprocess=False)}
        if isinstance(qtwt, type("")):
            qtwt = {t: w for t, w in self.tw.weights(self.tw.split(qtwt), preprocess=False)}
        s = 1e-9
        for k, v in qtwt.items():
            if k in dtwt:
                s += v  # * dtwt[k]
        q = 1e-9
        for k, v in qtwt.items():
            q += v
        return s / q

    def paragraph(self, content_tks: str, keywords: list = [], keywords_topn=30):
        """
        为一个段落内容生成一个全文检索查询表达式。

        该方法主要用于“相关推荐”或“更多相似段落”等场景，
        它会分析段落内容，提取关键词、同义词，并构建一个
        `MatchTextExpr`查询对象。

        Args:
            content_tks (str): 段落的文本内容（已分词，以空格分隔）。
            keywords (list, optional): 额外注入的关键词。Defaults to [].
            keywords_topn (int, optional): 从段落中提取的topN关键词数量。Defaults to 30.

        Returns:
            MatchTextExpr: 封装了最终查询逻辑的查询对象。
        """
        if isinstance(content_tks, str):
            content_tks = [c.strip() for c in content_tks.strip() if c.strip()]
        tks_w = self.tw.weights(content_tks, preprocess=False)

        keywords = [f'"{k.strip()}"' for k in keywords]
        for tk, w in sorted(tks_w, key=lambda x: x[1] * -1)[:keywords_topn]:
            tk_syns = self.syn.lookup(tk)
            tk_syns = [FulltextQueryer.subSpecialChar(s) for s in tk_syns]
            tk_syns = [rag_tokenizer.fine_grained_tokenize(s) for s in tk_syns if s]
            tk_syns = [f"\"{s}\"" if s.find(" ") > 0 else s for s in tk_syns]
            tk = FulltextQueryer.subSpecialChar(tk)
            if tk.find(" ") > 0:
                tk = '"%s"' % tk
            if tk_syns:
                tk = f"({tk} OR (%s)^0.2)" % " ".join(tk_syns)
            if tk:
                keywords.append(f"{tk}^{w}")

        return MatchTextExpr(self.query_fields, " ".join(keywords), 100,
                             {"minimum_should_match": min(3, len(keywords) / 10)})

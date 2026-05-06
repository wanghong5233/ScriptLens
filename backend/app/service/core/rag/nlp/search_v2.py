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
import re
from dataclasses import dataclass

from service.core.rag.settings import TAG_FLD, PAGERANK_FLD
from service.core.rag.utils import rmSpace
from service.core.rag.nlp import rag_tokenizer, query
import numpy as np
from service.core.rag.utils.doc_store_conn import DocStoreConnection, MatchDenseExpr, FusionExpr, OrderByExpr
from service.core.rag.nlp.model import generate_embedding, rerank_similarity

def index_name(uid): return f"{uid}"


class Dealer:
    def __init__(self, dataStore: DocStoreConnection):
        """
        Dealer类的构造函数。

        Args:
            dataStore (DocStoreConnection): 一个实现了与底层数据存储（如Elasticsearch）
                                            通信接口的对象。所有数据库操作都将通过
                                            这个对象进行。
        """
        self.qryr = query.FulltextQueryer()
        self.dataStore = dataStore

    @dataclass
    class SearchResult:
        total: int
        ids: list[str]
        query_vector: list[float] | None = None
        field: dict | None = None
        highlight: dict | None = None
        aggregation: list | dict | None = None
        keywords: list[str] | None = None
        group_docs: list[list] | None = None

    def get_vector(self, txt, emb_mdl, topk=10, similarity=0.1):
        """
        将文本转换为向量，并构建一个用于向量检索的表达式对象。

        Args:
            txt (str): 需要转换为向量的输入文本。
            embd_mdl: (在此实现中未直接使用) Embedding模型实例。
            topk (int): 指定向量检索应返回的最相似结果的数量。
            similarity (float): 相似度阈值，低于此阈值的结果将被过滤。

        Returns:
            MatchDenseExpr: 一个封装了向量检索查询所需全部信息的对象。
        """
        qv = generate_embedding(txt)
        shape = np.array(qv).shape
        if len(shape) > 1:
            raise Exception(
                f"Dealer.get_vector returned array's shape {shape} doesn't match expectation(exact one dimension).")
        embedding_data = [float(v) for v in qv]
        vector_column_name = f"q_{len(embedding_data)}_vec"
        # 封装为 MatchDenseExpr 对象，这是一个高级的、抽象的查询表示。
        # 目的在于“解耦”：上层业务逻辑（Dealer）只负责定义“查询意图”
        # （要查哪个字段、用什么向量、TopK是多少），而不需要关心底层
        # 数据库（如Elasticsearch）的具体查询语法（如拼接JSON）。
        # 真正的语法翻译工作由更底层的 dataStore 模块负责，从而实现了
        # 灵活性和可移植性。
        return MatchDenseExpr(vector_column_name, embedding_data, 'float', 'cosine', topk, {"similarity": similarity})

    def get_filters(self, req):
        """
        根据API请求，构造用于数据存储查询的精确过滤条件。

        此方法的作用等同于构建SQL查询中的`WHERE`子句，用于在执行
        模糊的文本/向量搜索之前，将搜索范围缩小到一个精确的数据子集中。

        处理流程:
        1. 定义一个从API请求键到数据库字段的映射（如`kb_ids` -> `kb_id`）。
        2. 遍历这个映射和一组预定义的其他过滤字段。
        3. 检查请求字典`req`中是否存在这些键，如果存在，则将其添加到
           `condition`字典中。
        4. 返回`condition`字典，该字典将用于数据存储层的精确匹配过滤。

        Args:
            req (dict): 包含过滤条件的API请求字典。

        Returns:
            dict: 一个键为数据库字段名，值为过滤值的字典，用作查询的`WHERE`条件。
        """
        condition = dict()
        for key, field in {"kb_ids": "kb_id", "doc_ids": "doc_id"}.items():
            if key in req and req[key] is not None:
                condition[field] = req[key]
        # TODO(yzc): `available_int` is nullable however infinity doesn't support nullable columns.
        for key in ["knowledge_graph_kwd", "available_int", "entity_kwd", "from_entity_kwd", "to_entity_kwd", "removed_kwd"]:
            if key in req and req[key] is not None:
                condition[key] = req[key]
        return condition

    def search(self, req, idx_names: str | list[str],
               kb_ids: list[str],
               emb_mdl=None,
               highlight=False,
               rank_feature: dict | None = None
               ):
        """
        负责执行初步召回，构造并向Elasticsearch发送一个混合查询请求。

        处理流程:
        1.  **准备查询参数**: 包括分页信息(`offset`, `limit`)、过滤条件(`filters`)
            以及需要返回的字段(`src`)。

        2.  **构建文本查询**: 调用 `self.qryr.question` 对用户问题进行分析，
            生成一个基于关键词的全文检索查询 `matchText`。这是混合查询的第一部分。

        3.  **构建向量查询**: 调用 `self.get_vector` 方法，该方法会：
            a. 将用户问题转换为查询向量。
            b. 构造一个向量相似度查询 `matchDense` (如k-NN查询)。
            这是混合查询的第二部分。

        4.  **定义融合策略**: 创建一个 `FusionExpr` 对象，指定如何将文本查询和
            向量查询的结果进行融合（如 `weighted_sum` 加权求和）。

        5.  **执行查询**: 将上述三个部分（`matchText`, `matchDense`, `fusionExpr`）
            组合成一个查询列表 `matchExprs`，并调用底层的 `self.dataStore.search`
            方法，由它将这个高级查询表示翻译成Elasticsearch的原生JSON查询语句
            并发送。

        6.  **结果处理**: 如果初次查询结果为空，会尝试降低关键词匹配的阈值
            (`min_match`)并进行重试，以提高召回率。

        7.  **返回结构化结果**: 将从Elasticsearch返回的原始结果解析并封装成
            一个 `SearchResult` 对象，其中包含了文档ID、高亮片段、聚合信息等。

        Args:
            req (dict): 包含查询参数的请求字典。
            idx_names (list[str]): 目标索引名称列表。
            kb_ids (list[str]): 知识库ID，用于过滤。
            emb_mdl: Embedding模型实例。
            highlight (bool): 是否需要返回高亮结果。

        Returns:
            SearchResult: 一个封装了初步召回结果的数据对象。
        """
        # 1. 参数准备：解析请求，准备过滤、排序和分页参数。
        filters = self.get_filters(req)
        # 初始化一个排序规则构建器，其作用等同于SQL的`ORDER BY`子句。
        # 它主要用于在没有相关度排序的场景下（如无问题浏览），为结果提供一个默认的顺序。
        orderBy = OrderByExpr()

        pg = int(req.get("page", 1)) - 1
        topk = int(req.get("topk", 1024))
        ps = int(req.get("size", topk))
        # 说明：若上游未设置 req["page"]（前N页精排策略），这里默认按第1页处理 -> offset=0，配合较大 size 做宽召回；
        #       若上游已设置 req["page"]/req["size"]（后续页直出策略），则按传入值计算 offset/limit，将分页下沉到存储层。
        offset, limit = pg * ps, ps

        src = req.get("fields",
                      ["docnm_kwd", "content_ltks", "kb_id", "img_id", "title_tks", "important_kwd", "position_int",
                       "doc_id", "page_num_int", "top_int", "create_timestamp_flt", "knowledge_graph_kwd",
                       "question_kwd", "question_tks",
                       "available_int", "content_with_weight", PAGERANK_FLD, TAG_FLD])
        kwds = set([])

        qst = req.get("question", "")
        q_vec = []
        # 处理无问题查询的特殊情况（例如，仅按筛选条件浏览知识库）
        if not qst:
            # 在此场景下，没有相关度可言，因此检查请求中是否需要应用默认排序。
            if req.get("sort"):
                # 添加多层排序规则：首先按文档页码升序，其次按页面位置升序，最后按时间降序。
                orderBy.asc("page_num_int")
                orderBy.asc("top_int")
                orderBy.desc("create_timestamp_flt")
            # 使用构建好的过滤和排序规则执行查询。
            # 这行代码是Python向Elasticsearch数据库发出底层查询请求的核心指令。
            # - src: 指定返回哪些字段 (类似于SQL的 SELECT)
            # - []: (highlightFields) 在此场景下不进行关键词高亮。
            # - filters: 过滤条件 (类似于SQL的 WHERE)
            # - []: (matchExprs) 在此场景下没有相关度查询 (因为没有问题)。
            # - orderBy: 排序规则 (类似于SQL的 ORDER BY)
            # - offset, limit: 分页参数 (类似于SQL的 OFFSET 和 LIMIT)
            # - idx_names: 目标索引 (类似于SQL的 FROM table)
            # - kb_ids: 知识库ID，会被合并到过滤条件中。
            res = self.dataStore.search(src, [], filters, [], orderBy, offset, limit, idx_names, kb_ids)
            total = self.dataStore.getTotal(res)
            logging.debug("Dealer.search TOTAL: {}".format(total))
        else:
            # 2. 查询构建：为有问题的查询构造混合查询的各个部分。
            highlightFields = ["content_ltks", "title_tks"] if highlight else []
            
            # 2.1 构建文本查询 (Keyword Search part)
            matchText, keywords = self.qryr.question(qst, min_match=0.3)
            
            # 2.2 构建向量查询 (Vector Search part)
            matchDense = self.get_vector(qst, emb_mdl, topk, req.get("similarity", 0.1))
            q_vec = matchDense.embedding_data
            src.append(f"q_{len(q_vec)}_vec") # 确保向量字段也被返回
            
            # 2.3 构建融合策略 (Fusion part)
            fusionExpr = FusionExpr("weighted_sum", topk, {"weights": "0.05, 0.95"})
            
            # 3. 查询执行：将三部分组合并发送给数据存储层。
            matchExprs = [matchText, matchDense, fusionExpr]
            res = self.dataStore.search(src, highlightFields, filters, matchExprs, orderBy, offset, limit,
                                        idx_names, kb_ids, rank_feature=rank_feature)
            total = self.dataStore.getTotal(res)
            logging.debug("Dealer.search TOTAL: {}".format(total))

            # 4. 失败重试：如果首次查询无结果，则放宽条件重试一次。
            if total == 0:
                # 放宽文本匹配要求和向量相似度阈值
                matchText, _ = self.qryr.question(qst, min_match=0.1)
                filters.pop("doc_ids", None)
                matchDense.extra_options["similarity"] = 0.17
                res = self.dataStore.search(src, highlightFields, filters, [matchText, matchDense, fusionExpr],
                                            orderBy, offset, limit, idx_names, kb_ids, rank_feature=rank_feature)
                total = self.dataStore.getTotal(res)
                logging.debug("Dealer.search 2 TOTAL: {}".format(total))

            for k in keywords:
                kwds.add(k)
                for kk in rag_tokenizer.fine_grained_tokenize(k).split():
                    if len(kk) < 2:
                        continue
                    if kk in kwds:
                        continue
                    kwds.add(kk)

        # 5. 结果封装：从数据库原始响应中解析并提取所需信息。
        logging.debug(f"TOTAL: {total}")
        ids = self.dataStore.getChunkIds(res)
        keywords = list(kwds)
        highlight = self.dataStore.getHighlight(res, keywords, "content_with_weight")
        
# # 高亮前
# "人工智能技术在各个领域都有广泛的应用前景"

# # 高亮后  
# "<em>人工智能</em>技术在各个领域都有广泛的应用前景"

        # 提取聚合（Aggregation）结果。
        # “聚合”在搜索引擎中等同于SQL的 `GROUP BY` 操作，用于统计分析。
        # 这里的指令是：“请根据文档名称（`docnm_kwd`）对所有搜索到的
        # 文本块（chunks）进行分组，并计算每个文档名下有多少个文本块”。
        #
        # 示例：
        # 如果搜索结果包含来自 `report_A.pdf` 的5个块和 `manual_B.docx` 的3个块，
        # 那么 `aggs` 的值将会是：
        # [ ("report_A.pdf", 5), ("manual_B.docx", 3) ]
        #
        # 这个统计结果对于在前端UI上展示“引用来源”列表至关重要，
        # 它清晰地告诉用户答案主要参考了哪些文档及其贡献度。
        aggs = self.dataStore.getAggregation(res, "docnm_kwd")
        return self.SearchResult(
            total=total,
            ids=ids,
            query_vector=q_vec,
            aggregation=aggs,
            highlight=highlight,
            field=self.dataStore.getFields(res, src),
            keywords=keywords
        )

    @staticmethod
    def trans2floats(txt):
        return [float(t) for t in txt.split("\t")]

    def insert_citations(self, answer, chunks, chunk_v,
                         embd_mdl, tkweight=0.1, vtweight=0.9):
        """
        在LLM生成的答案中，智能地插入来源引用标记。

        这是一个后处理步骤，旨在将答案的各个部分与其所依据的知识库内容
        （chunks）进行关联，提升答案的可信度和可追溯性。

        处理流程:
        1.  将答案文本切分成句子或逻辑片段。
        2.  为每个片段生成向量。
        3.  计算每个片段与所有检索到的 `chunks` 之间的混合相似度。
        4.  如果某个片段与某个 `chunk` 的相似度超过阈值，就在该片段末尾
            插入指向该 `chunk` 的引用标记（如 `##1$$`）。
        5.  将所有处理过的片段重新组合成带引用标记的最终答案。

        Args:
            answer (str): LLM生成的原始答案。
            chunks (list[str]): 检索阶段返回的、作为上下文的知识块文本列表。
            chunk_v (list[list[float]]): 与 `chunks` 对应的向量列表。
            embd_mdl: Embedding模型实例，用于为答案片段生成向量。
            tkweight (float): 文本相似度的权重。
            vtweight (float): 向量相似度的权重。

        Returns:
            tuple[str, set]: 一个元组，包含带引用标记的答案字符串和所有被引用的chunk索引集合。
        """
        assert len(chunks) == len(chunk_v)
        if not chunks:
            return answer, set([])
        pieces = re.split(r"(```)", answer)
        if len(pieces) >= 3:
            i = 0
            pieces_ = []
            while i < len(pieces):
                if pieces[i] == "```":
                    st = i
                    i += 1
                    while i < len(pieces) and pieces[i] != "```":
                        i += 1
                    if i < len(pieces):
                        i += 1
                    pieces_.append("".join(pieces[st: i]) + "\n")
                else:
                    pieces_.extend(
                        re.split(
                            r"([^\|][；。？!！\n]|[a-z][.?;!][ \n])",
                            pieces[i]))
                    i += 1
            pieces = pieces_
        else:
            pieces = re.split(r"([^\|][；。？!！\n]|[a-z][.?;!][ \n])", answer)
        for i in range(1, len(pieces)):
            if re.match(r"([^\|][；。？!！\n]|[a-z][.?;!][ \n])", pieces[i]):
                pieces[i - 1] += pieces[i][0]
                pieces[i] = pieces[i][1:]
        idx = []
        pieces_ = []
        for i, t in enumerate(pieces):
            if len(t) < 5:
                continue
            idx.append(i)
            pieces_.append(t)
        logging.debug("{} => {}".format(answer, pieces_))
        if not pieces_:
            return answer, set([])

        ans_v, _ = embd_mdl.encode(pieces_)
        for i in range(len(chunk_v)):
            if len(ans_v[0]) != len(chunk_v[i]):
                chunk_v[i] = [0.0]*len(ans_v[0])
                logging.warning("The dimension of query and chunk do not match: {} vs. {}".format(len(ans_v[0]), len(chunk_v[i])))

        assert len(ans_v[0]) == len(chunk_v[0]), "The dimension of query and chunk do not match: {} vs. {}".format(
            len(ans_v[0]), len(chunk_v[0]))

        chunks_tks = [rag_tokenizer.tokenize(self.qryr.rmWWW(ck)).split()
                      for ck in chunks]
        cites = {}
        thr = 0.63
        while thr > 0.3 and len(cites.keys()) == 0 and pieces_ and chunks_tks:
            for i, a in enumerate(pieces_):
                sim, tksim, vtsim = self.qryr.hybrid_similarity(ans_v[i],
                                                                chunk_v,
                                                                rag_tokenizer.tokenize(
                                                                    self.qryr.rmWWW(pieces_[i])).split(),
                                                                chunks_tks,
                                                                tkweight, vtweight)
                mx = np.max(sim) * 0.99
                logging.debug("{} SIM: {}".format(pieces_[i], mx))
                if mx < thr:
                    continue
                cites[idx[i]] = list(
                    set([str(ii) for ii in range(len(chunk_v)) if sim[ii] > mx]))[:4]
            thr *= 0.8

        res = ""
        seted = set([])
        for i, p in enumerate(pieces):
            res += p
            if i not in idx:
                continue
            if i not in cites:
                continue
            for c in cites[i]:
                assert int(c) < len(chunk_v)
            for c in cites[i]:
                if c in seted:
                    continue
                res += f" ##{c}$$"
                seted.add(c)

        return res, seted

    def _rank_feature_scores(self, query_rfea, search_res):
        """
        (高级功能) 计算基于排名特征（如标签、PageRank）的附加分数。

        此方法用于实现更复杂的排名策略，它可以根据查询中包含的特定
        特征（`query_rfea`）和文档自身的特征（存储在 `TAG_FLD` 字段），
        计算出一个额外的排名分数。

        Args:
            query_rfea (dict): 查询中包含的排名特征及其权重。
            search_res (SearchResult): 初步召回的结果。

        Returns:
            numpy.ndarray: 一个包含每个召回文档的附加特征分数的数组。
        """
        ## For rank feature(tag_fea) scores.
        rank_fea = []
        pageranks = []
        for chunk_id in search_res.ids:
            pageranks.append(search_res.field[chunk_id].get(PAGERANK_FLD, 0))
        pageranks = np.array(pageranks, dtype=float)

        if not query_rfea:
            return np.array([0 for _ in range(len(search_res.ids))]) + pageranks

        q_denor = np.sqrt(np.sum([s*s for t,s in query_rfea.items() if t != PAGERANK_FLD]))
        for i in search_res.ids:
            nor, denor = 0, 0
            for t, sc in eval(search_res.field[i].get(TAG_FLD, "{}")).items():
                if t in query_rfea:
                    nor += query_rfea[t] * sc
                denor += sc * sc
            if denor == 0:
                rank_fea.append(0)
            else:
                rank_fea.append(nor/np.sqrt(denor)/q_denor)
        return np.array(rank_fea)*10. + pageranks

    def rerank(self, sres, query, tkweight=0.3,
               vtweight=0.7, cfield="content_ltks",
               rank_feature: dict | None = None
               ):
        """
        在应用层对初步召回的结果进行二次精排。

        与在数据库中进行的初步融合排序不同，`rerank` 在应用服务的内存中
        进行，可以使用更复杂、更灵活的计分逻辑，从而获得比数据库召回
        更精准的排序结果。

        处理流程:
        1.  **数据提取**: 从初步召回结果 `sres` 中提取出每个候选文档的
            向量(`ins_embd`)和经过加权处理（如标题、关键词权重更高）的
            分词列表(`ins_tw`)。

        2.  **计算混合相似度**: 调用 `self.qryr.hybrid_similarity` 方法，
            该方法会独立计算并融合以下两种相似度：
            - **向量相似度 (Vector Similarity)**: 计算用户问题向量与每个
              文档向量之间的余弦相似度。
            - **文本相似度 (Token Similarity)**: 计算用户问题的关键词与
              每个文档加权分词列表之间的相似度（如BM25算法的变体）。

        3.  **加权融合**: 根据传入的 `tkweight` (文本权重) 和 `vtweight`
            (向量权重)，将上述两种相似度得分进行加权求和，得到最终的
            综合排序分 `sim`。

        4.  **返回多种得分**: 返回最终的综合分 `sim`，以及独立的文本分 `tsim`
            和向量分 `vsim`，以供上层进行分析或展示。

        Args:
            sres (SearchResult): 初步召回的结果对象。
            query (str): 用户的原始查询问题。
            tkweight (float): 文本相似度得分的权重。
            vtweight (float): 向量相似度得分的权重。

        Returns:
            tuple: 包含三种得分的元组 (sim, tsim, vsim)。
        """
        _, keywords = self.qryr.question(query)
        vector_size = len(sres.query_vector)
        vector_column = f"q_{vector_size}_vec"
        zero_vector = [0.0] * vector_size
        ins_embd = []
        for chunk_id in sres.ids:
            vector = sres.field[chunk_id].get(vector_column, zero_vector)
            if isinstance(vector, str):
                vector = [float(v) for v in vector.split("\t")]
            ins_embd.append(vector)
        if not ins_embd:
            return [], [], []

        for i in sres.ids:
            if isinstance(sres.field[i].get("important_kwd", []), str):
                sres.field[i]["important_kwd"] = [sres.field[i]["important_kwd"]]
        ins_tw = []
        for i in sres.ids:
            content_ltks = sres.field[i][cfield].split()
            title_tks = [t for t in sres.field[i].get("title_tks", "").split() if t]
            question_tks = [t for t in sres.field[i].get("question_tks", "").split() if t]
            important_kwd = sres.field[i].get("important_kwd", [])
            tks = content_ltks + title_tks * 2 + important_kwd * 5 + question_tks * 6
            ins_tw.append(tks)

        ## For rank feature(tag_fea) scores.
        rank_fea = self._rank_feature_scores(rank_feature, sres)

        sim, tksim, vtsim = self.qryr.hybrid_similarity(sres.query_vector,
                                                        ins_embd,
                                                        keywords,
                                                        ins_tw, tkweight, vtweight)

        return sim + rank_fea, tksim, vtsim

    def rerank_by_model(self, rerank_mdl, sres, query, tkweight=0.3,
                        vtweight=0.7, cfield="content_ltks",
                        rank_feature: dict | None = None):
        """
        使用专门的重排模型对召回结果进行二次精排。

        与 `rerank` 方法相比，此方法依赖一个外部的、更强大的重排模型
        (`rerank_mdl`)来计算文档与查询的相关度，通常能达到比基于
        规则的 `rerank` 方法更好的效果。

        处理流程:
        1.  数据准备，提取候选文档的文本内容。
        2.  调用 `rerank_similarity` 函数，该函数会与重排模型服务交互，
            获取模型计算出的相关度分数 `vtsim`。
        3.  同时，计算传统的文本相似度 `tksim`。
        4.  根据权重融合模型分数和文本相似度分数，并加上高级特征分数，
            得到最终的综合排序分。

        Args:
            rerank_mdl: (在此实现中可能通过 `rerank_similarity` 间接使用) 重排模型实例。
            sres (SearchResult): 初步召回的结果对象。
            query (str): 用户的原始查询问题。
            tkweight (float): 文本相似度得分的权重。
            vtweight (float): 向量/模型相似度得分的权重。
            cfield (str): 指定用于计算文本相似度的内容字段。
            rank_feature (dict, optional): 用于高级排名计算的附加特征。

        Returns:
            tuple: 包含三种得分的元组 (sim, tsim, vsim)。
        """
        _, keywords = self.qryr.question(query)

        for i in sres.ids:
            if isinstance(sres.field[i].get("important_kwd", []), str):
                sres.field[i]["important_kwd"] = [sres.field[i]["important_kwd"]]
        ins_tw = []
        for i in sres.ids:
            content_ltks = sres.field[i][cfield].split()
            title_tks = [t for t in sres.field[i].get("title_tks", "").split() if t]
            important_kwd = sres.field[i].get("important_kwd", [])
            tks = content_ltks + title_tks + important_kwd
            ins_tw.append(tks)

        tksim = self.qryr.token_similarity(keywords, ins_tw)
        vtsim, _ = rerank_similarity(query, [rmSpace(" ".join(tks)) for tks in ins_tw])
        ## For rank feature(tag_fea) scores.
        rank_fea = self._rank_feature_scores(rank_feature, sres)

        return tkweight * (np.array(tksim)+rank_fea) + vtweight * vtsim, tksim, vtsim

    def hybrid_similarity(self, ans_embd, ins_embd, ans, inst):
        """
        一个辅助方法，封装了对 `query.FulltextQueryer.hybrid_similarity` 的调用。

        主要用于计算两组文本之间的混合相似度得分。

        Args:
            ans_embd: 查询文本的向量。
            ins_embd: 候选文本的向量列表。
            ans: 查询文本的原文。
            inst: 候选文本的原文。

        Returns:
            混合相似度得分。
        """
        return self.qryr.hybrid_similarity(ans_embd,
                                           ins_embd,
                                           rag_tokenizer.tokenize(ans).split(),
                                           rag_tokenizer.tokenize(inst).split())

    def retrieval(self, question, embd_mdl, tenant_ids, kb_ids, page, page_size, similarity_threshold=0.1,
                  vector_similarity_weight=0.3, top=1024, doc_ids=None, aggs=True,
                  rerank_mdl=None, highlight=False,
                  rank_feature: dict | None = {PAGERANK_FLD: 10}):
        """
        执行完整的两阶段检索流程（召回 + 精排），是整个检索功能的核心调度器。

        处理流程:
        1.  **准备请求**: 构造一个用于初步检索（召回）的请求字典 `req`。
            为了给后续的精排提供足够多的高质量候选，召回阶段会获取比最终
            所需数量更多的文档（`page_size * RERANK_PAGE_LIMIT`）。

        2.  **执行初步召回**: 调用 `self.search` 方法，向Elasticsearch
            发起一个混合查询（关键词+向量），获取一批初步排序的候选文档 `sres`。

        3.  **执行二次精排**:
            - 如果有专门的重排模型 (`rerank_mdl`)，则调用 `rerank_by_model`
              利用模型对召回结果进行更精准的语义相关度排序。
            - 否则，调用 `rerank` 方法，在应用层面对召回结果进行基于规则
              和加权算法的混合相似度重计算，得到新的排序。

        4.  **应用最终排序和分页**:
            - 使用 `numpy.argsort` 根据精排后的综合相似度得分 `sim` 对结果
              进行倒序排列。
            - 根据请求的 `page` 和 `page_size` 对排序后的结果进行切片，
              实现分页功能。

        5.  **组装返回结果**: 遍历最终排序后的顶级文档，提取其详细信息
            （如内容、来源、相似度得分等），并组装成一个结构化的字典 `ranks`
            返回给上层服务。

        Args:
            question (str): 用户的原始查询问题。
            embd_mdl: Embedding模型实例，传递给下游函数使用。
            tenant_ids (list[str]): 租户/用户ID列表，用于确定检索的索引范围。
            kb_ids (list[str]): 知识库ID列表，用于在索引内进一步过滤。
            page (int): 请求的**检索结果分页**的页码。这是一个决定数据“偏移量”
                        的关键参数，用于获取不同页的结果，而非一次性获取所有。
                        例如，`page=1, page_size=5`返回最相关的1-5条结果；
                        `page=2, page_size=5`则通过偏移计算，返回最相关的6-10条结果。
            page_size (int): 每页返回的**检索结果**数量。在单次请求中，其效果等同于
                             Top-K中的'K'，但与`page`参数配合使用以实现分页。
            similarity_threshold (float): 最终结果的相似度阈值，低于此值将被过滤。
            vector_similarity_weight (float): 在 `rerank` 中，向量相似度的权重。
            top (int): 初步召回阶段，向量检索返回的最多候选数量。
            doc_ids (list[str], optional): 限定在这些文档ID内进行检索。
            aggs (bool): 是否需要返回聚合信息。
            rerank_mdl: (可选) 专门用于重排序的模型。
            highlight (bool): 是否需要返回高亮结果。
            rank_feature (dict, optional): 用于高级排名计算的附加特征。


        Returns:
            dict: 一个包含检索结果的字典，结构如：
                  {
                      "total": int,
                      "chunks": list[dict],
                      "doc_aggs": list[dict]
                  }
        """
        ranks = {"total": 0, "chunks": [], "doc_aggs": {}}

        RERANK_PAGE_LIMIT = 3
        req = {"kb_ids": kb_ids, "doc_ids": doc_ids, "size": max(page_size * RERANK_PAGE_LIMIT, 128),
               "question": question, "vector": True, "topk": top,
               "similarity": similarity_threshold,
               "available_int": 1}

        # --- 性能优化：对不同页码采用不同检索策略 ---
        # `RERANK_PAGE_LIMIT` 是一个分界线，定义了精排功能生效的最高页码（如前3页）。
        #
        # 1. 高质量精排模式 (当 page <= RERANK_PAGE_LIMIT):
        #    `if`条件不成立。此处不设置 `req["page"]`，并沿用上面构造 `req` 时设置的较大 `size`
        #    （如 max(page_size * RERANK_PAGE_LIMIT, 128)）做“宽召回”，为后续应用层 `rerank`/`rerank_by_model`
        #    的高质量排序做准备。
        #
        # 2. 高效率直出模式 (当 page > RERANK_PAGE_LIMIT):
        #    `if`条件成立。程序切换到高效率模式：
        #    - `req["page"] = page` / `req["size"] = page_size`：将分页责任下沉到数据存储层；
        #    - 随后跳过应用层精排，直接使用数据存储层基于混合查询（matchText + matchDense + FusionExpr）的排序结果。
        if page > RERANK_PAGE_LIMIT:
            req["page"] = page
            req["size"] = page_size

        # 参数格式化：确保 tenant_ids 是一个列表。
        # 这是一个健壮性处理，允许上层调用者传入单个ID字符串或
        # 逗号分隔的ID字符串，这里会统一转换成列表格式以满足
        # 底层 search 方法的参数要求。
        if isinstance(tenant_ids, str):
            tenant_ids = tenant_ids.split(",")

        # 调用 self.search 方法，执行初步的混合检索（召回）。
        # - [index_name(tid) for tid in tenant_ids]: 将租户/用户ID列表转换为
        #   Elasticsearch实际的索引名称列表。
        # - 其他参数：将上层传递的参数继续向下传递。
        sres = self.search(req, [index_name(tid) for tid in tenant_ids],
                           kb_ids, embd_mdl, highlight, rank_feature=rank_feature)
        # 将初步召回阶段，Elasticsearch认为可能相关的文档总数，存储到最终返回结果中。
        # 这个`total`值将用于前端UI，以正确地渲染分页组件（例如，计算总页数）。
        ranks["total"] = sres.total


        # --- 策略分岔口：根据页码决定是否执行精排 ---
        # 说明：这里用的是函数入参变量 `page` 来决定走哪种策略（应用层精排 or 存储层直出）。
        # - 当 page <= RERANK_PAGE_LIMIT：不将分页下沉到存储层；使用前面构造的“较大 size”做宽召回，
        #   在应用层精排后，再用 `page/page_size` 在内存中切片。
        # - 当 page >  RERANK_PAGE_LIMIT：前面已把 `req["page"]`/`req["size"]` 传给存储层，
        #   将分页下沉，跳过应用层精排以提升性能。
        if page <= RERANK_PAGE_LIMIT:
            if sres.total > 0:
                print("重排模型。。。。")
                sim, tsim, vsim = self.rerank_by_model(rerank_mdl,
                                                       sres, question, 1 - vector_similarity_weight,
                                                       vector_similarity_weight,
                                                       rank_feature=rank_feature)
            else:
                # 调用启发式精排方法，该方法不依赖外部模型，速度快。
                # 它会在应用层，根据预设权重，将向量相似度和关键词相似度进行加权求和，
                # 得到一个最终的综合排序分数。
                #
                # sim (SIMilarity): 最终混合相似度。它是 vsim 和 tsim 的加权总分，
                #                 是后续排序的唯一依据。
                # tsim (Term SIMilarity): 词项相似度。衡量问题关键词与文档关键词的重合度。
                # vsim (Vector SIMilarity): 向量相似度。衡量问题向量与文档向量在语义空间的接近度。
                sim, tsim, vsim = self.rerank(
                    sres, question, 1 - vector_similarity_weight, vector_similarity_weight,
                    rank_feature=rank_feature)
            
            # --- 分页与最终结果截取 ---
            # 关键逻辑澄清：
            # 1.【一次提问，一个总排名】：`np.argsort` 对当前这【一个问题】的所有召回结果
            #    进行一次性排序，生成本次请求内的“总排行榜”(`idx`)；不同请求会重新计算。
            # 2.【`page`参数决定偏移量】：`page` 用于计算从 `idx` 中截取的起始位置，`page_size` 决定截取长度。
            # 3.【返回固定数量】：系统每次仅返回 `page_size` 条结果，绝非 `page*page_size` 条。
            idx = np.argsort(sim * -1)[(page - 1) * page_size:page * page_size]
        else:
            sim = tsim = vsim = [1] * len(sres.ids)
            idx = list(range(len(sres.ids)))

        dim = len(sres.query_vector)
        vector_column = f"q_{dim}_vec"
        zero_vector = [0.0] * dim

        # --- 第三步：格式化输出 ---
        # 经过二次精排和分页后，我们得到了 `idx`，这是一个包含了“当前页”
        # 所有文档块在原始搜索结果 `sres` 中“位置索引”的列表。
        #
        # 现在，我们遍历这个 `idx` 列表，根据这些位置索引，从 `sres` 这份
        # 丰富的“原始资料包”中，提取出所需信息，并组装成最终的返回格式。
        #
        # 变量解析：
        # - sres (SearchResult Object): 一个包含了全部初步召回结果（例如128个）
        #   的富信息对象。可以看作是一个“资料包”，其主要属性有：
        #   - sres.ids: list[str]， 存储了所有候选块的ID。
        #   - sres.field: dict[str, dict]，一个以块ID为键，块详细信息为值的字典。
        #
        # - idx (list[int]): 一个经过精排和分页后的“位置索引”列表。它的长度
        #   等于 page_size。列表中的每个整数，都指向了最终排名靠前的某个块
        #   在 `sres.ids` 和 `sim` 数组中的原始位置。
        #   例如：idx = [5, 1, 8] 意味着，最终排名前三的块，分别是原始结果中
        #   的第5、第1和第8个块。
        #
        # - sim, tsim, vsim (numpy.ndarray): 与原始结果 `sres` 一一对应的、
        #   包含了128个文档块的各类相似度得分数组。
        #
        # 接下来的循环 `for i in idx:` 就是根据 `idx` 中的位置指引，
        # 从 `sres` 和 `sim` 中精确地挑出最终胜出的那几个文档块的信息。
        for i in idx:
            # 如果当前块的最终相似度得分低于设定的阈值，则后续的块得分只会更低，
            # 因此可以直接跳出循环，不再处理。
            if sim[i] < similarity_threshold:
                break
            # 这是一个双重保障，确保最终返回的`chunks`数量不会超过`page_size`。
            # 理论上，前面的分页切片已经保证了这一点，但这里的判断让代码更健壮。
            if len(ranks["chunks"]) >= page_size:
                if aggs:
                    continue
                break
            
            # 根据索引 `i`，从原始搜索结果 `sres` 中获取该块的唯一ID。
            id = sres.ids[i]
            # 根据ID，从原始搜索结果 `sres` 中获取该块的所有字段信息。
            chunk = sres.field[id]
            # 获取文档名称 (dnm 是 document name 的缩写)。`_kwd`后缀表示这是一个keyword类型的字段。
            dnm = chunk.get("docnm_kwd", "")
            # 获取该块所属的文档的唯一ID。
            did = chunk.get("doc_id", "")
            # 获取该块在原始文档中的位置信息（如页码、坐标等）。
            position_int = chunk.get("position_int", [])
            # 开始组装一个标准化的、用于返回给上层服务的 chunk 字典。
            d = {
                # 文本块的唯一ID
                "chunk_id": id,
                # 文本块内容（ltks 是 Large Tokens 的缩写，表示粗粒度分词的结果）
                "content_ltks": chunk["content_ltks"],
                # 文本块内容（带权重信息，通常用于显示）
                "content_with_weight": chunk["content_with_weight"],
                # 文档的唯一ID
                "doc_id": did,
                # 文档名称
                "docnm_kwd": dnm,
                # 知识库ID
                "kb_id": chunk["kb_id"],
                # 该块中的重要关键词列表
                "important_kwd": chunk.get("important_kwd", []),
                # 如果该块关联了图片，则为图片ID
                "image_id": chunk.get("img_id", ""),
                # 最终混合相似度得分 (sim = term_sim * w + vector_sim * (1-w))
                "similarity": sim[i],
                # 纯向量语义相似度得分
                "vector_similarity": vsim[i],
                # 纯关键词词项相似度得分
                "term_similarity": tsim[i],
                # 该文本块自身的向量表示
                "vector": chunk.get(vector_column, zero_vector),
                # 该文本块在源文档中的物理位置信息
                "positions": position_int,
            }
            # 如果请求需要高亮，并且搜索结果中确实包含了高亮信息
            if highlight and sres.highlight:
                # 如果当前块有高亮片段
                if id in sres.highlight:
                    # 将高亮片段（通常是被<em></em>标签包裹的HTML）存入字典
                    d["highlight"] = rmSpace(sres.highlight[id])
                else:
                    # 如果没有，则使用原始文本作为高亮内容
                    d["highlight"] = d["content_with_weight"]
            # 将组装好的字典添加到最终结果的 `chunks` 列表中
            ranks["chunks"].append(d)
            
            # 开始构建聚合信息，即按文档名称进行分组计数 (类似 SQL 的 GROUP BY)
            # 这部分数据用于在前端展示“引用来源”列表。
            # 如果文档名是第一次出现
            if dnm not in ranks["doc_aggs"]:
                # 初始化该文档的计数器
                ranks["doc_aggs"][dnm] = {"doc_id": did, "count": 0}
            # 将该文档的计数值加一
            ranks["doc_aggs"][dnm]["count"] += 1
        # 将聚合结果的字典，转换为一个按计数值（count）降序排列的列表，
        # 方便前端直接使用。
        ranks["doc_aggs"] = [{"doc_name": k,
                              "doc_id": v["doc_id"],
                              "count": v["count"]} for k,
                                                       v in sorted(ranks["doc_aggs"].items(),
                                                                   key=lambda x: x[1]["count"] * -1)]
        # 再次确保返回的 chunks 数量不超过 page_size，作为最终保障。
        ranks["chunks"] = ranks["chunks"][:page_size]

        return ranks
# 返回结果如下
# ranks = {
#     "total": 总匹配数量,
#     "chunks": [
#         {
#             "chunk_id": "chunk_123",
#             "content_with_weight": "匹配的文本内容...",
#             "similarity": 0.85,
#             "vector_similarity": 0.78,
#             "term_similarity": 0.92,
#             "doc_id": "doc_456",
#             "docnm_kwd": "技术文档",
#             "highlight": "<em>高亮</em>的匹配文本...",
#             "positions": [(1, 100, 200, 50, 70)],
#             # ... 其他字段
#         }
#     ],
#     "doc_aggs": [
#         {"doc_name": "AI技术报告.pdf", "doc_id": "doc_456", "count": 5},
#         {"doc_name": "机器学习指南.docx", "doc_id": "doc_789", "count": 3}
#     ]
# }  


    def sql_retrieval(self, sql, fetch_size=128, format="json"):
        """
        (替代功能) 直接通过SQL语句从数据存储中检索数据。

        此方法提供了一个绕过标准RAG检索流程的接口，允许直接对底层
        数据（如果支持SQL查询的话）执行结构化查询。

        Args:
            sql (str): 要执行的SQL查询语句。
            fetch_size (int): 每次从数据库获取的行数。
            format (str): 返回结果的格式。

        Returns:
            查询结果。
        """
        tbl = self.dataStore.sql(sql, fetch_size, format)
        return tbl

    def chunk_list(self, doc_id: str, tenant_id: str,
                   kb_ids: list[str], max_count=1024,
                   offset=0,
                   fields=["docnm_kwd", "content_with_weight", "img_id"]):
        """
        (工具功能) 获取指定文档ID下的所有知识块列表。

        Args:
            doc_id (str): 目标文档的ID。
            tenant_id (str): 租户/用户ID。
            kb_ids (list[str]): 知识库ID列表。
            max_count (int): 最多返回的知识块数量。
            offset (int): 返回结果的偏移量。
            fields (list[str]): 需要返回的字段列表。

        Returns:
            list[dict]: 包含知识块信息的字典列表。
        """
        condition = {"doc_id": doc_id}
        res = []
        bs = 128
        for p in range(offset, max_count, bs):
            es_res = self.dataStore.search(fields, [], condition, [], OrderByExpr(), p, bs, index_name(tenant_id),
                                           kb_ids)
            dict_chunks = self.dataStore.getFields(es_res, fields)
            for id, doc in dict_chunks.items():
                doc["id"] = id
            if dict_chunks:
                res.extend(dict_chunks.values())
            if len(dict_chunks.values()) < bs:
                break
        return res

    def all_tags(self, tenant_id: str, kb_ids: list[str], S=1000):
        """
        (高级功能) 获取指定知识库范围内的所有标签及其计数。

        Args:
            tenant_id (str): 租户/用户ID。
            kb_ids (list[str]): 知识库ID列表。
            S (int): (可能未使用) 一个平滑参数。

        Returns:
            聚合后的标签及其计数值。
        """
        res = self.dataStore.search([], [], {}, [], OrderByExpr(), 0, 0, index_name(tenant_id), kb_ids, ["tag_kwd"])
        return self.dataStore.getAggregation(res, "tag_kwd")

    def all_tags_in_portion(self, tenant_id: str, kb_ids: list[str], S=1000):
        """
        (高级功能) 获取所有标签，并计算它们在知识库中的占比。

        Args:
            tenant_id (str): 租户/用户ID。
            kb_ids (list[str]): 知识库ID列表。
            S (int): 一个平滑参数，防止除以零。

        Returns:
            dict: 一个字典，键为标签，值为其在知识库中的占比。
        """
        res = self.dataStore.search([], [], {}, [], OrderByExpr(), 0, 0, index_name(tenant_id), kb_ids, ["tag_kwd"])
        res = self.dataStore.getAggregation(res, "tag_kwd")
        total = np.sum([c for _, c in res])
        return {t: (c + 1) / (total + S) for t, c in res}

    def tag_content(self, tenant_id: str, kb_ids: list[str], doc, all_tags, topn_tags=3, keywords_topn=30, S=1000):
        """
        (高级功能) 为单个文档内容打上最相关的标签。

        通过对文档内容进行关键词检索，找出与之最匹配的已有标签。

        Args:
            tenant_id (str): 租户/用户ID。
            kb_ids (list[str]): 知识库ID列表。
            doc (dict): 需要打标签的文档对象。
            all_tags (dict): 包含所有标签及其占比的字典。
            topn_tags (int): 最多为文档打上的标签数量。

        Returns:
            bool: 如果成功打上标签则返回True，否则返回False。
        """
        idx_nm = index_name(tenant_id)
        match_txt = self.qryr.paragraph(doc["title_tks"] + " " + doc["content_ltks"], doc.get("important_kwd", []), keywords_topn)
        res = self.dataStore.search([], [], {}, [match_txt], OrderByExpr(), 0, 0, idx_nm, kb_ids, ["tag_kwd"])
        aggs = self.dataStore.getAggregation(res, "tag_kwd")
        if not aggs:
            return False
        cnt = np.sum([c for _, c in aggs])
        tag_fea = sorted([(a, round(0.1*(c + 1) / (cnt + S) / max(1e-6, all_tags.get(a, 0.0001)))) for a, c in aggs],
                         key=lambda x: x[1] * -1)[:topn_tags]
        doc[TAG_FLD] = {a: c for a, c in tag_fea if c > 0}
        return True

    def tag_query(self, question: str, tenant_ids: str | list[str], kb_ids: list[str], all_tags, topn_tags=3, S=1000):
        """
        (高级功能) 根据用户问题，预测最相关的标签。

        这可以用于查询扩展或生成用于高级排名的特征。

        Args:
            question (str): 用户的查询问题。
            tenant_ids (list[str]): 租户/用户ID列表。
            kb_ids (list[str]): 知识库ID列表。
            all_tags (dict): 包含所有标签及其占比的字典。
            topn_tags (int): 最多返回的相关标签数量。

        Returns:
            dict: 一个字典，键为预测的标签，值为其相关度得分。
        """
        if isinstance(tenant_ids, str):
            idx_nms = index_name(tenant_ids)
        else:
            idx_nms = [index_name(tid) for tid in tenant_ids]
        match_txt, _ = self.qryr.question(question, min_match=0.0)
        res = self.dataStore.search([], [], {}, [match_txt], OrderByExpr(), 0, 0, idx_nms, kb_ids, ["tag_kwd"])
        aggs = self.dataStore.getAggregation(res, "tag_kwd")
        if not aggs:
            return {}
        cnt = np.sum([c for _, c in aggs])
        tag_fea = sorted([(a, round(0.1*(c + 1) / (cnt + S) / max(1e-6, all_tags.get(a, 0.0001)))) for a, c in aggs],
                         key=lambda x: x[1] * -1)[:topn_tags]
        return {a: max(1, c) for a, c in tag_fea}

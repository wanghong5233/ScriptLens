from . import rag_tokenizer
import re
import copy
from collections import Counter
import logging
import chardet
from service.core.rag.utils import num_tokens_from_string
from PIL import Image

def is_english(texts):
    eng = 0
    if not texts:
        return False
    for t in texts:
        if re.match(r"[ `a-zA-Z.,':;/\"?<>!\(\)-]", t.strip()):
            eng += 1
    if eng / len(texts) > 0.8:
        return True
    return False

def tokenize_chunks_docx(chunks, doc, eng, images):
    res = []
    # wrap up as es documents
    for ck, image in zip(chunks, images):
        if len(ck.strip()) == 0:
            continue
        logging.debug("-- {}".format(ck))
        d = copy.deepcopy(doc)
        d["image"] = image
        tokenize(d, ck, eng)
        res.append(d)
    return res
    
def concat_img(img1, img2):
    if img1 and not img2:
        return img1
    if not img1 and img2:
        return img2
    if not img1 and not img2:
        return None
    width1, height1 = img1.size
    width2, height2 = img2.size

    new_width = max(width1, width2)
    new_height = height1 + height2
    new_image = Image.new('RGB', (new_width, new_height))

    new_image.paste(img1, (0, 0))
    new_image.paste(img2, (0, height1))

    return new_image


def naive_merge_docx(sections, chunk_token_num=128, delimiter="\n。；！？"):
    if not sections:
        return [], []

    cks = [""]
    images = [None]
    tk_nums = [0]

    def add_chunk(t, image, pos=""):
        nonlocal cks, tk_nums, delimiter
        tnum = num_tokens_from_string(t)
        if tnum < 8:
            pos = ""
        if tk_nums[-1] > chunk_token_num:
            if t.find(pos) < 0:
                t += pos
            cks.append(t)
            images.append(image)
            tk_nums.append(tnum)
        else:
            if cks[-1].find(pos) < 0:
                t += pos
            cks[-1] += t
            images[-1] = concat_img(images[-1], image)
            tk_nums[-1] += tnum

    for sec, image in sections:
        add_chunk(sec, image, '')

    return cks, images


def naive_merge(sections, chunk_token_num=128, delimiter="\n。；！？"):
    """
    一个朴素的文本合并函数，负责将解析器返回的零散文本行（sections）
    智能地聚合成大小适中的、语义连贯的文本块（chunks）。

    该函数是RAG数据处理流程中的“承上启下”环节，上游连接各类文档解析器，
    下游为最终的向量化和索引做数据准备。其核心目标是在遵循Token数量限制的前提下，
    尽可能地保持文本的原始段落结构和语义完整性。

    处理逻辑:
    1.  **初始化**: 创建一个空的文本块列表 `cks` 和一个对应的Token数列表 `tk_nums`。
        `cks` 中的第一个元素被初始化为空字符串，作为第一个“容器”来接收文本。

    2.  **迭代与累加**: 遍历 `sections` 列表中的每一个文本行 `(sec, pos)`。
        `sec` 是文本内容，`pos` 是其在文档中的元信息（如页码、布局类型标签）。

    3.  **容量检查与决策**:
        - `add_chunk` 内部函数会计算当前 `cks` 最后一个容器的Token数 (`tk_nums[-1]`)。
        - 如果这个容器的Token数已经超过了 `chunk_token_num` 的阈值，
          意味着这个容器“已满”。此时，会将新的文本行 `t` 作为一个全新的块
          添加到 `cks` 列表中，并相应地更新 `tk_nums`。
        - 如果容器“未满”，则直接将新的文本行 `t` 追加到当前容器的末尾，
          并累加其Token数到 `tk_nums[-1]`。

    4.  **元信息处理**: `pos` 元信息会被追加到文本块的末尾，以便在后续步骤中
        （如 `tokenize_chunks`）可以利用这些信息。

    5.  **处理超长行 (隐式)**: 如果单个 `sec` 的Token数就超过了 `chunk_token_num`，
        它会被作为一个独立的块存入 `cks`，后续的 `sec` 会被放入一个新的块中。
        虽然函数名是 "naive" (朴素的)，但这种基于容量的累加策略是构建
        高质量知识块（Chunk）的基础。

    Args:
        sections (list[tuple[str, str]] or list[str]): 
            一个列表，包含了从文档解析器中提取出的所有文本片段。
            - 如果是 `list[tuple[str, str]]`，每个元组代表 `(文本内容, 元信息)`。
            - 如果是 `list[str]`，会被自动转换为 `(文本内容, "")` 的形式。

        chunk_token_num (int): 
            每个文本块（chunk）的目标最大Token数。这是一个软限制，最终的块大小
            可能会略微超过这个值。这是控制知识库颗粒度的核心参数。

        delimiter (str): 
            (在此函数中未直接使用，但在其调用的下游函数或相似逻辑中可能使用)
            用于切分超长文本行的分隔符，通常是标点符号。

    Returns:
        list[str]: 
        一个字符串列表，其中每个字符串都是一个合并好的、准备进行下一步处理的
        文本块 (chunk)。
    """
    if not sections:
        return []
    if isinstance(sections[0], type("")):
        sections = [(s, "") for s in sections]
    cks = [""]
    tk_nums = [0]

    def add_chunk(t, pos):
        nonlocal cks, tk_nums, delimiter
        tnum = num_tokens_from_string(t)
        if not pos:
            pos = ""
        if tnum < 8:
            pos = ""
        # Ensure that the length of the merged chunk does not exceed chunk_token_num  
        if tk_nums[-1] > chunk_token_num:

            if t.find(pos) < 0:
                t += pos
            cks.append(t)
            tk_nums.append(tnum)
        else:
            if cks[-1].find(pos) < 0:
                t += pos
            cks[-1] += t
            tk_nums[-1] += tnum

    for sec, pos in sections:
        add_chunk(sec, pos)

    return cks


all_codecs = [
    'utf-8', 'gb2312', 'gbk', 'utf_16', 'ascii', 'big5', 'big5hkscs',
    'cp037', 'cp273', 'cp424', 'cp437',
    'cp500', 'cp720', 'cp737', 'cp775', 'cp850', 'cp852', 'cp855', 'cp856', 'cp857',
    'cp858', 'cp860', 'cp861', 'cp862', 'cp863', 'cp864', 'cp865', 'cp866', 'cp869',
    'cp874', 'cp875', 'cp932', 'cp949', 'cp950', 'cp1006', 'cp1026', 'cp1125',
    'cp1140', 'cp1250', 'cp1251', 'cp1252', 'cp1253', 'cp1254', 'cp1255', 'cp1256',
    'cp1257', 'cp1258', 'euc_jp', 'euc_jis_2004', 'euc_jisx0213', 'euc_kr',
    'gb2312', 'gb18030', 'hz', 'iso2022_jp', 'iso2022_jp_1', 'iso2022_jp_2',
    'iso2022_jp_2004', 'iso2022_jp_3', 'iso2022_jp_ext', 'iso2022_kr', 'latin_1',
    'iso8859_2', 'iso8859_3', 'iso8859_4', 'iso8859_5', 'iso8859_6', 'iso8859_7',
    'iso8859_8', 'iso8859_9', 'iso8859_10', 'iso8859_11', 'iso8859_13',
    'iso8859_14', 'iso8859_15', 'iso8859_16', 'johab', 'koi8_r', 'koi8_t', 'koi8_u',
    'kz1048', 'mac_cyrillic', 'mac_greek', 'mac_iceland', 'mac_latin2', 'mac_roman',
    'mac_turkish', 'ptcp154', 'shift_jis', 'shift_jis_2004', 'shift_jisx0213',
    'utf_32', 'utf_32_be', 'utf_32_le', 'utf_16_be', 'utf_16_le', 'utf_7', 'windows-1250', 'windows-1251',
    'windows-1252', 'windows-1253', 'windows-1254', 'windows-1255', 'windows-1256',
    'windows-1257', 'windows-1258', 'latin-2'
]

BULLET_PATTERN = [[
    r"第[零一二三四五六七八九十百0-9]+(分?编|部分)",
    r"第[零一二三四五六七八九十百0-9]+章",
    r"第[零一二三四五六七八九十百0-9]+节",
    r"第[零一二三四五六七八九十百0-9]+条",
    r"[\(（][零一二三四五六七八九十百]+[\)）]",
], [
    r"第[0-9]+章",
    r"第[0-9]+节",
    r"[0-9]{,2}[\. 、]",
    r"[0-9]{,2}\.[0-9]{,2}[^a-zA-Z/%~-]",
    r"[0-9]{,2}\.[0-9]{,2}\.[0-9]{,2}",
    r"[0-9]{,2}\.[0-9]{,2}\.[0-9]{,2}\.[0-9]{,2}",
], [
    r"第[零一二三四五六七八九十百0-9]+章",
    r"第[零一二三四五六七八九十百0-9]+节",
    r"[零一二三四五六七八九十百]+[ 、]",
    r"[\(（][零一二三四五六七八九十百]+[\)）]",
    r"[\(（][0-9]{,2}[\)）]",
], [
    r"PART (ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN)",
    r"Chapter (I+V?|VI*|XI|IX|X)",
    r"Section [0-9]+",
    r"Article [0-9]+"
]
]


def find_codec(blob):
    detected = chardet.detect(blob[:1024])
    if detected['confidence'] > 0.5:
        return detected['encoding']

    for c in all_codecs:
        try:
            blob[:1024].decode(c)
            return c
        except Exception:
            pass
        try:
            blob.decode(c)
            return c
        except Exception:
            pass

    return "utf-8"


def tokenize(d, t, eng):
    d["content_with_weight"] = t
    t = re.sub(r"</?(table|td|caption|tr|th)( [^<>]{0,12})?>", " ", t)
    d["content_ltks"] = rag_tokenizer.tokenize(t)
    d["content_sm_ltks"] = rag_tokenizer.fine_grained_tokenize(d["content_ltks"])

def add_positions(d, poss):
    if not poss:
        return
    page_num_int = []
    position_int = []
    top_int = []
    for pn, left, right, top, bottom in poss:
        page_num_int.append(int(pn + 1))
        top_int.append(int(top))
        position_int.append((int(pn + 1), int(left), int(right), int(top), int(bottom)))
    d["page_num_int"] = page_num_int
    d["position_int"] = position_int
    d["top_int"] = top_int

def tokenize_table(tbls, doc, eng, batch_size=10):
    res = []
    # add tables
    for (img, rows), poss in tbls:
        if not rows:
            continue
        if isinstance(rows, str):
            d = copy.deepcopy(doc)
            tokenize(d, rows, eng)
            d["content_with_weight"] = rows
            if img:
                d["image"] = img
            if poss:
                add_positions(d, poss)
            res.append(d)
            continue
        de = "; " if eng else "； "
        for i in range(0, len(rows), batch_size):
            d = copy.deepcopy(doc)
            r = de.join(rows[i:i + batch_size])
            tokenize(d, r, eng)
            d["image"] = img
            add_positions(d, poss)
            res.append(d)
    return res

def not_bullet(line):
    patt = [
        r"0", r"[0-9]+ +[0-9~个只-]", r"[0-9]+\.{2,}"
    ]
    return any([re.match(r, line) for r in patt])

def bullets_category(sections):
    global BULLET_PATTERN
    hits = [0] * len(BULLET_PATTERN)
    for i, pro in enumerate(BULLET_PATTERN):
        for sec in sections:
            for p in pro:
                if re.match(p, sec) and not not_bullet(sec):
                    hits[i] += 1
                    break
    maxium = 0
    res = -1
    for i, h in enumerate(hits):
        if h <= maxium:
            continue
        res = i
        maxium = h
    return res


def title_frequency(bull, sections):
    bullets_size = len(BULLET_PATTERN[bull])
    levels = [bullets_size+1 for _ in range(len(sections))]
    if not sections or bull < 0:
        return bullets_size+1, levels

    for i, (txt, layout) in enumerate(sections):
        for j, p in enumerate(BULLET_PATTERN[bull]):
            if re.match(p, txt.strip()) and not not_bullet(txt):
                levels[i] = j
                break
        else:
            if re.search(r"(title|head)", layout) and not not_title(txt.split("@")[0]):
                levels[i] = bullets_size
    most_level = bullets_size+1
    for level, c in sorted(Counter(levels).items(), key=lambda x:x[1]*-1):
        if level <= bullets_size:
            most_level = level
            break
    return most_level, levels

def tokenize_chunks(chunks, doc, eng, pdf_parser=None):
    """
    对合并好的文本块（chunks）进行最终的“精加工”，生成可直接存入向量数据库的标准格式。

    这个函数是数据处理流水线的最后一站（在生成向量之前）。它负责将纯文本的 chunks
    转化为包含丰富元数据和多层次分词结果的结构化字典。

    处理逻辑:
    1.  **遍历Chunks**: 对输入的 `chunks` 列表进行循环处理，每个 `ck` 代表一个文本块。

    2.  **元数据继承**: 使用 `copy.deepcopy(doc)`，让每个 chunk 都继承
        文档级别的元数据（如文件名 `docnm_kwd`、标题分词 `title_tks` 等）。
        这是实现后续按元数据过滤检索的基础。

    3.  **图像与位置提取 (针对PDF)**:
        - 如果提供了 `pdf_parser` 对象，会尝试调用其 `crop` 方法。
        - `crop` 方法能够根据文本块的内容 `ck`，从原始PDF中裁剪出对应的
          **图像快照**，并获取其精确的物理**位置坐标** `poss` (页码、左上右下坐标)。
        - 提取出的图像和位置信息会被添加到当前 chunk 的字典 `d` 中。
        - 之后，会从文本 `ck` 中移除用于定位的内部标签。

    4.  **文本分词与格式化**:
        - 调用 `tokenize` 辅助函数，对文本块 `ck` 进行处理。
        - `tokenize` 内部会：
          - 将原始文本存入 `d["content_with_weight"]`。
          - 对文本进行**粗粒度分词**，结果存入 `d["content_ltks"]`。
          - 在粗粒度分词的基础上，进行**细粒度分词**，结果存入 `d["content_sm_ltks"]`。

    5.  **构建最终结果**: 将处理完毕的、结构化的字典 `d` 添加到结果列表 `res` 中。

    Args:
        chunks (list[str]): 
            由 `naive_merge` 函数生成的文本块列表。
        
        doc (dict): 
            一个包含文档级别元数据的字典，将被复制到每个chunk中。
        
        eng (bool): 
            一个布尔标志，指示文本是否为英文，会影响分词策略。
        
        pdf_parser (PdfParser, optional): 
            一个PDF解析器实例。如果提供，将用于从PDF中提取与chunk
            对应的图像和位置信息。默认为None。

    Returns:
        list[dict]: 
        一个列表，其中每个元素都是一个处理完成的chunk的字典。
        该字典已包含所有必要信息，可以直接用于后续的向量生成和索引构建。
    """
    res = []
    # wrap up as es documents
    for ck in chunks:
        if len(ck.strip()) == 0:
            continue
        logging.debug("-- {}".format(ck))
        d = copy.deepcopy(doc)
        if pdf_parser:
            try:
                d["image"], poss = pdf_parser.crop(ck, need_position=True)
                add_positions(d, poss)
                ck = pdf_parser.remove_tag(ck)
            except NotImplementedError:
                pass
        tokenize(d, ck, eng)
        res.append(d)
    return res


def docx_question_level(p, bull = -1):
    txt = re.sub(r"\u3000", " ", p.text).strip()
    if p.style.name.startswith('Heading'):
        return int(p.style.name.split(' ')[-1]), txt
    else:
        if bull < 0:
            return 0, txt
        for j, title in enumerate(BULLET_PATTERN[bull]):
            if re.match(title, txt):
                return j+1, txt
    return len(BULLET_PATTERN[bull]), txt
FROM python:3.11.7-slim

# ----------------------------------------------------------------------------
# APT 源：使用阿里云 mirror（ECS 内网访问，免公网流量、速度 100+MB/s）
#   原因：之前用清华公网源（mirrors.tuna.tsinghua.edu.cn）在阿里云 ECS 上
#         走公网出口（小机器带宽 1-3 Mbps），装 build-essential 时拖到
#         15+ 分钟仍未结束、被 timeout 杀掉。doc_studio 用阿里云内网源
#         装 4 GB+ 的 TeX Live 都能成功 —— 验证了内网链路才是关键。
#   注意：python:3.11.7-slim 基于 Debian 12 (bookworm)，使用新格式的
#         /etc/apt/sources.list.d/debian.sources（deb822）。如不删除该文件，
#         APT 会同时尝试官方源 deb.debian.org，污染我们的源选择。所以先删
#         deb822 文件，再写一个干净的 sources.list 只指向阿里云。
# ----------------------------------------------------------------------------
RUN rm -f /etc/apt/sources.list.d/debian.sources && \
    echo "deb http://mirrors.aliyun.com/debian/ bookworm main contrib non-free non-free-firmware" > /etc/apt/sources.list && \
    echo "deb http://mirrors.aliyun.com/debian/ bookworm-updates main contrib non-free non-free-firmware" >> /etc/apt/sources.list && \
    echo "deb http://mirrors.aliyun.com/debian-security bookworm-security main contrib non-free non-free-firmware" >> /etc/apt/sources.list

# 安装必要的构建工具
# 已移除 libgl1 / libgl1-mesa-glx / libglu1-mesa / libglib2.0-0 / make / curl
#   - 前四个是 opencv-python (cv2) 的 GUI/视频后端依赖；cv2 已从 requirements
#     移除（详见 components_factory 顶部说明），这些 lib 是死重 (~100 MB)。
#   - make / curl 不再被任何 RUN 步骤需要。
RUN apt-get update --fix-missing || apt-get update && \
    apt-get install -y --no-install-recommends --fix-missing \
    build-essential \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# ----------------------------------------------------------------------------
# pip 源：阿里云 PyPI 镜像（ECS 内网访问，理由同上）
#   原 cernet 源走公网，新源走阿里云内网，pip install 阶段速度提升明显。
# ----------------------------------------------------------------------------
RUN pip config set global.index-url "https://mirrors.aliyun.com/pypi/simple/" && \
    pip config set install.trusted-host "mirrors.aliyun.com" && \
    pip install --upgrade pip setuptools wheel

# 设置工作目录
WORKDIR /app

# 拷贝依赖文件并安装
# 注意：因为构建上下文改为了 backend/ 目录，所以源路径需要加上 app/ 前缀
COPY app/requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝代码
# 将 app/ 目录下的所有内容拷贝到容器的 /app 目录
COPY app/ /app

# NLTK data must not be fetched during production builds: ECS does not use the
# developer machine's VPN, and the upstream NLTK data host can hang indefinitely.
# Runtime tokenization has a regex fallback when punkt/wordnet data is absent.
ENV NLTK_DATA=/usr/local/nltk_data
RUN mkdir -p /usr/local/nltk_data

# 修改文件权限
RUN chmod -R 755 /app
RUN chmod +x /app/start.sh

# 设置启动脚本为入口点
ENTRYPOINT ["/app/start.sh"]

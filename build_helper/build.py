# SPDX-FileCopyrightText: Copyright (c) 2024-2025 沉默の金 <cmzj@cmzj.org>
# SPDX-License-Identifier: MIT
import os
import re
import shutil
import tarfile
import zipfile

import zstandard as zstd
from actions_toolkit import core
from actions_toolkit.github import Context

from .utils.logger import logger
from .utils.openwrt import ImageBuilder, OpenWrt
from .utils.paths import paths
from .utils.repo import del_cache, dl_artifact
from .utils.upload import uploader
from .utils.utils import hash_dirs, setup_env


def get_cache_restore_key(openwrt: OpenWrt, cfg: dict) -> str:
    context = Context()
    if context.job.startswith("base-builds"):
        job_prefix = "base-builds"
    elif context.job.startswith("build-packages"):
        job_prefix = "build-packages"
    elif context.job.startswith("build-ImageBuilder"):
        job_prefix = "build-ImageBuilder"
    else:
        msg = "Invalid job"
        raise ValueError(msg)
    cache_restore_key = f"{job_prefix}-{cfg["compile"]["openwrt_tag/branch"]}-{cfg["name"]}"
    target, subtarget = openwrt.get_target()
    if target:
        cache_restore_key += f"-{target}"
    if subtarget:
        cache_restore_key += f"-{subtarget}"
    return cache_restore_key


def prepare(cfg: dict) -> None:
    context = Context()
    logger.debug("job: %s", context.job)
    setup_env(context.job in ("build-packages", "build-ImageBuilder", "build-images-releases"),
              context.job in ("build-packages", "build-ImageBuilder", "build-images-releases"))

    tmpdir = paths.get_tmpdir()

    logger.info("还原openwrt源码...")
    path = dl_artifact(f"openwrt-source-{cfg["name"]}", tmpdir.name)
    with zipfile.ZipFile(path, "r") as zip_ref:
        zip_ref.extract("openwrt-source.tar.gz", tmpdir.name)
    with tarfile.open(os.path.join(tmpdir.name, "openwrt-source.tar.gz"), "r") as tar_ref:
        tar_ref.extractall(paths.workdir)  # noqa: S202
    openwrt = OpenWrt(os.path.join(paths.workdir, "openwrt"))

    if context.job == "base-builds":
        logger.info("构建toolchain缓存key...")
        toolchain_key = f"toolchain-{hash_dirs((os.path.join(openwrt.path, "tools"), os.path.join(openwrt.path, "toolchain")))}"
        target, subtarget = openwrt.get_target()
        if target:
            toolchain_key += f"-{target}"
        if subtarget:
            toolchain_key += f"-{subtarget}"
        core.set_output("toolchain-key", toolchain_key)

    elif context.job in ("build-packages", "build-ImageBuilder"):
        if os.path.exists(os.path.join(openwrt.path, "staging_dir")):
            shutil.rmtree(os.path.join(openwrt.path, "staging_dir"))
        base_builds_path = dl_artifact(f"base-builds-{cfg['name']}", tmpdir.name)
        with zipfile.ZipFile(base_builds_path, "r") as zip_ref:
            zip_ref.extract("builds.tar.gz", tmpdir.name)
        with tarfile.open(os.path.join(tmpdir.name, "builds.tar.gz"), "r:gz") as tar:
            tar.extractall(openwrt.path)  # noqa: S202

    elif context.job == "build-images-releases":
        ib_path = dl_artifact(f"Image_Builder-{cfg["name"]}", tmpdir.name)
        with zipfile.ZipFile(ib_path, "r") as zip_ref:
            ext = "zst" if "openwrt-imagebuilder.tar.zst" in zip_ref.namelist() else "xz"
            zip_ref.extract(f"openwrt-imagebuilder.tar.{ext}", tmpdir.name)

        ib_path = os.path.join(tmpdir.name, f"openwrt-imagebuilder.tar.{ext}")

        if ext == "zst":
            with open(ib_path, 'rb') as f:
                dctx = zstd.ZstdDecompressor()
                with dctx.stream_reader(f) as reader, tarfile.open(fileobj=reader, mode="r|*") as tar:
                    tar.extractall(paths.workdir)  # noqa: S202
                    shutil.move(os.path.join(paths.workdir, tar.getnames()[0]), os.path.join(paths.workdir, "ImageBuilder"))
        else:
            with tarfile.open(os.path.join(tmpdir.name, "openwrt-imagebuilder.tar.xz"), "r:xz") as tar:
                tar.extractall(paths.workdir)  # noqa: S202
                shutil.move(os.path.join(paths.workdir, tar.getnames()[0]), os.path.join(paths.workdir, "ImageBuilder"))

        ib = ImageBuilder(os.path.join(paths.workdir, "ImageBuilder"))

        pkgs_path = dl_artifact(f"packages-{cfg['name']}", tmpdir.name)
        with zipfile.ZipFile(pkgs_path, "r") as zip_ref:
            for membber in zip_ref.infolist():
                if not os.path.exists(os.path.join(ib.packages_path, membber.filename)) and not membber.is_dir():
                    with zip_ref.open(membber) as f, open(os.path.join(ib.packages_path, os.path.basename(membber.filename)), "wb") as fw:
                        shutil.copyfileobj(f, fw)
                        logger.debug("解压文件 %s到 %s", membber.filename, os.path.join(ib.packages_path, os.path.basename(membber.filename)))

        shutil.copytree(os.path.join(openwrt.path, "files"), os.path.join(ib.path, "files"))
        if os.path.exists(os.path.join(ib.path, ".config")):
            os.remove(os.path.join(ib.path, ".config"))
        shutil.copy2(os.path.join(openwrt.path, ".config"), os.path.join(ib.path, ".config"))

    else:
        msg = f"未知的工作流 {context.job}"
        raise ValueError(msg)

    if context.job in ("base-builds", "build-packages", "build-ImageBuilder"):
        cache_restore_key = get_cache_restore_key(openwrt, cfg)
        core.set_output("cache-key", f"{cache_restore_key}-{context.run_id}")
        core.set_output("cache-restore-key", cache_restore_key)
    core.set_output("use-cache", cfg["compile"]["use_cache"])
    core.set_output("openwrt-path", openwrt.path)

def base_builds(cfg: dict) -> None:
    openwrt = OpenWrt(os.path.join(paths.workdir, "openwrt"))

    ccache_path = os.path.join(openwrt.path, ".ccache")
    tmp_ccache_path = None
    if os.path.exists(ccache_path):
        tmp_ccache_path = paths.get_tmpdir()
        os.replace(ccache_path, tmp_ccache_path.name)

    logger.info("修改配置(设置编译所有kmod)...")
    openwrt.enable_kmods(cfg["compile"]["kmod_compile_exclude_list"])

    if os.getenv("CACHE_HIT", "").lower().strip() != "true":
        logger.info("下载编译工具链所需源码...")
        openwrt.download_source("tools/download")
        openwrt.download_source("target/prereq")
        openwrt.download_source("toolchain/download")
        logger.info("开始编译tools...")
        openwrt.make("tools/install")
        logger.info("开始编译toolchain...")
        openwrt.make("toolchain/install")
        logger.info("正在清理...")
        openwrt.make("clean")

    logger.info("下载编译内核所需源码...")
    openwrt.download_source("target/download")
    logger.info("开始编译内核...")
    if tmp_ccache_path:
        if os.path.exists(ccache_path):
            shutil.rmtree(ccache_path)
        os.replace(tmp_ccache_path.name, ccache_path)
        tmp_ccache_path.cleanup()
    openwrt.make("target/compile")

    logger.info("归档文件...")
    tar_path = os.path.join(paths.uploads, "builds.tar.gz")
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(os.path.join(openwrt.path, "staging_dir"), arcname="staging_dir")
        tar.add(os.path.join(openwrt.path, "build_dir"), arcname="build_dir")
    uploader.add(f"base-builds-{cfg["name"]}", tar_path, retention_days=1, compression_level=0)

    logger.info("删除旧缓存...")
    del_cache(get_cache_restore_key(openwrt, cfg))


def build_packages(cfg: dict) -> None:
    openwrt = OpenWrt(os.path.join(paths.workdir, "openwrt"))

    logger.info("下载编译所需源码...")
    openwrt.download_source()

    logger.info("开始编译软件包...")
    openwrt.make("package/compile")

    logger.info("开始生成软件包...")
    openwrt.make("package/install")

    logger.info("整理软件包...")
    packages_path = os.path.join(paths.uploads, "packages")
    os.makedirs(packages_path, exist_ok=True)
    for root, _dirs, files in os.walk(os.path.join(openwrt.path, "bin")):
        for file in files:
            if file.endswith(".ipk"):
                shutil.copy2(os.path.join(root, file), packages_path)
                logger.debug(f"复制 {file} 到 {packages_path}")
    uploader.add(f"packages-{cfg['name']}", packages_path, retention_days=1)

    logger.info("删除旧缓存...")
    del_cache(get_cache_restore_key(openwrt, cfg))

def build_image_builder(cfg: dict) -> None:
    openwrt = OpenWrt(os.path.join(paths.workdir, "openwrt"))

    logger.info("修改配置(设置编译所有kmod/取消编译其他软件包/取消生成镜像/)...")
    openwrt.enable_kmods(cfg["compile"]["kmod_compile_exclude_list"], only_kmods=True)
    with open(os.path.join(openwrt.path, ".config")) as f:
        config = f.read()
    with open(os.path.join(openwrt.path, ".config"), "w") as f:
        for line in config.splitlines():
            if ((match := re.match(r"CONFIG_(?P<name>[^_=]+)_IMAGES=y", line)) or
                (match := re.match(r"CONFIG_TARGET_ROOTFS_(?P<name>[^_=]+)=y", line)) or
                (match := re.match(r"CONFIG_TARGET_IMAGES_(?P<name>[^_=]+)=y", line))):
                name = match.group("name")
                if name in ("ISO", "VDI", "VMDK", "VHDX", "TARGZ", "CPIOGZ", "EXT4FS", "SQUASHFS", "GZIP"):   # 移除 SQUASHFS
                    logger.debug(f"不构建 {name} 格式镜像")
                    f.write(line.replace("=y", "=n") + "\n")
            else:
                f.write(line + "\n")
        f.write("CONFIG_IB=y\n")
        f.write("CONFIG_IB_STANDALONE=y\n")
        # f.write("CONFIG_SDK=y\n")
    openwrt.make_defconfig()

    logger.info("下载编译所需源码...")
    openwrt.download_source()

    logger.info("开始编译软件包...")
    openwrt.make("package/compile")

    logger.info("开始生成软件包...")
    openwrt.make("package/install")

    logger.info("制作Image Builder包...")
    openwrt.make("target/install")

    logger.info("制作包索引、镜像概述信息并计算校验和...")
    openwrt.make("package/index")
    openwrt.make("json_overview_image_info")
    openwrt.make("checksum")

    logger.info("整理kmods...")

    kmods_path = os.path.join(paths.uploads, "kmods")
    os.makedirs(kmods_path, exist_ok=True)
    for root, _dirs, files in os.walk(os.path.join(openwrt.path, "bin")):
        for file in files:
            if file.startswith("kmod-") and file.endswith(".ipk"):
                shutil.copy2(os.path.join(root, file), kmods_path)
                logger.debug(f"复制 {file} 到 {kmods_path}")
    uploader.add(f"kmods-{cfg['name']}", kmods_path, retention_days=1)

    target, subtarget = openwrt.get_target()
    if target is None or subtarget is None:
        msg = "无法获取target信息"
        raise RuntimeError(msg)


    # 设定目标路径
    target_dir = os.path.join(openwrt.path, "bin", "targets", target, subtarget)

    # 获取所有 .ubi 和 .bin 文件
    files_to_upload = [f for f in os.listdir(target_dir) if f.endswith((".ubi", ".bin"))]

    # 遍历文件并上传
    if not files_to_upload:
        logger.error("未找到符合条件的固件文件 (.ubi 或 .bin)")
        exit(1)

    for filename in files_to_upload:
        file_path = os.path.join(target_dir, filename)
    
        if os.path.exists(file_path):
            uploader.add(f"Image_Builder-{cfg['name']}", file_path, retention_days=1, compression_level=0)
            logger.info(f"成功上传文件: {filename}")
        else:
            logger.error(f"文件不存在: {file_path}")


    # 设定目标路径
    target_dir = os.path.join(openwrt.path, "bin", "targets", target, subtarget)

    # 正则匹配 "imagebuilder" 且以 "Linux-x86_64.tar.zst" 结尾的文件
    pattern = re.compile(r".*imagebuilder.*Linux-x86_64\.tar\.zst$")
    image_builder_files = [f for f in os.listdir(target_dir) if pattern.match(f)]

    # 找到符合条件的文件
    if not image_builder_files:
        logger.error("未找到符合条件的 Image Builder 文件")
        exit(1)

    # 选择第一个符合条件的文件
    bl_path = os.path.join(target_dir, image_builder_files[0])
    ext = "zst"

    # 移动文件到 uploads 目录
    upload_path = os.path.join(paths.uploads, os.path.basename(bl_path))
    try:
        shutil.move(bl_path, upload_path)
        bl_path = upload_path
        logger.info(f"文件已移动至: {upload_path}")
    except Exception as e:
        logger.error(f"文件移动失败: {e}")
        exit(1)

    # 添加到上传
    uploader.add(f"Image_Builder-{cfg['name']}", bl_path, retention_days=1, compression_level=0)

    # 清理缓存
    logger.info("删除旧缓存...")
    del_cache(get_cache_restore_key(openwrt, cfg))

def build_images(cfg: dict) -> None:
    ib = ImageBuilder(os.path.join(paths.workdir, "ImageBuilder"))

    logger.info("收集镜像信息...")
    ib.make_info()
    ib.make_manifest()

    logger.info("开始构建镜像...")
    ib.make_image()

    target, subtarget = ib.get_target()
    if target is None or subtarget is None:
        msg = "无法获取target信息"
        raise RuntimeError(msg)

    logger.info("准备上传...")
    uploader.add(f"firmware-{cfg['name']}", os.path.join(ib.path, "bin", "targets", target, subtarget), retention_days=1, compression_level=0)

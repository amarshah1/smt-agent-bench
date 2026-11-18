# Optional build/run environment for machines whose toolchain is too old for cvc5 or the Z3 release
# binaries (cvc5 needs GCC >= 10; Z3 release binaries need glibc >= 2.39). Use it via scripts/docker.sh.
FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential cmake ninja-build git curl ca-certificates unzip xz-utils pkg-config m4 \
      python3 python3-venv python3-pip python3-tomli python3-pyparsing libgmp-dev zlib1g-dev opam bubblewrap rsync \
    && rm -rf /var/lib/apt/lists/*
ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo ELAN_HOME=/opt/elan \
    PATH=/opt/cargo/bin:/opt/elan/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y -q --profile minimal --default-toolchain stable \
 && curl -sSfL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh | sh -s -- -y --default-toolchain none \
 && chmod -R a+rwX /opt/cargo /opt/rustup /opt/elan

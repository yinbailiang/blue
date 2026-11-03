{
  description = "blue —— Infinity 系统开发环境（uv + RabbitMQ）";

  inputs = {
    # 复用系统 registry 中的 nixpkgs（NixOS 26.05 自带，本机为 f3a73bc），
    # 无需访问 GitHub，适合网络受限环境。
    # 网络可用时可改成固定 rev 以便完全复现：
    #   nixpkgs.url = "github:NixOS/nixpkgs/f3a73bcf5d1c5b75a66cf35a48957e30d41d647a";
    nixpkgs.url = "nixpkgs";
  };

  outputs =
    { nixpkgs, ... }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
    in
    {
      devShells = nixpkgs.lib.genAttrs systems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          default = pkgs.mkShell {
            name = "blue";

            packages = with pkgs; [
              uv # Python 依赖 / 虚拟环境管理
              python314 # 与 .python-version 中的 3.14 对齐
              rabbitmq-server # RabbitMQ 4.x（自带 Erlang 运行时）
              git
              curl
              jq
            ];

            shellHook = ''
              export PROJECT_ROOT="''${PROJECT_ROOT:-$PWD}"
              export RABBITMQ_DATA_DIR="$PROJECT_ROOT/.rabbitmq"

              # 数据、日志、插件统一放在项目内，不污染用户目录
              export RABBITMQ_MNESIA_BASE="$RABBITMQ_DATA_DIR/mnesia"
              export RABBITMQ_LOG_BASE="$RABBITMQ_DATA_DIR/log"
              export RABBITMQ_PID_FILE="$RABBITMQ_DATA_DIR/pid"
              export RABBITMQ_ENABLED_PLUGINS_FILE="$RABBITMQ_DATA_DIR/enabled_plugins"

              # 只监听本机回环地址（AMQP 5672 / 管理台 15672）
              export RABBITMQ_CONFIG_FILE="$RABBITMQ_DATA_DIR/rabbitmq"

              # 应用侧默认连接串
              export RABBITMQ_URL="amqp://guest:guest@127.0.0.1:5672/"

              mkdir -p "$RABBITMQ_MNESIA_BASE" "$RABBITMQ_LOG_BASE"
              if [ ! -f "$RABBITMQ_CONFIG_FILE.conf" ]; then
                cat > "$RABBITMQ_CONFIG_FILE.conf" <<'RABBITMQ_CONF'
listeners.tcp.1 = 127.0.0.1:5672
management.tcp.ip = 127.0.0.1
RABBITMQ_CONF
              fi
              if [ ! -f "$RABBITMQ_ENABLED_PLUGINS_FILE" ]; then
                echo '[rabbitmq_management].' > "$RABBITMQ_ENABLED_PLUGINS_FILE"
              fi

              echo "blue devShell → uv $(uv --version | cut -d' ' -f2) | $(python3.14 --version) | RabbitMQ ${pkgs.rabbitmq-server.version}"
              echo "  uv sync            # 安装/同步 Python 依赖"
              echo "  rabbitmq-server    # 前台启动 RabbitMQ（管理台 http://127.0.0.1:15672，guest/guest）"
              echo "  rabbitmqctl status # 查看节点状态"
            '';
          };
        }
      );
    };
}

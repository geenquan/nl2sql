from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.metadata import TableMetadata


class DataSourceConnector(ABC):
    """数据源端口。所有数据库适配器都实现此接口，业务服务不依赖驱动细节。

    M1 接口调整：新增 `sample_table`，供画像模块以有界采样读取数据；
    只读约束由实现通过连接级设置与系统构造查询共同保证。
    """

    @abstractmethod
    def test_connection(self) -> bool:
        """验证连接；失败时抛出可处理的异常。"""

    @abstractmethod
    def list_schemas(self) -> list[str]:
        """列出当前账号有权访问的 Schema。"""

    @abstractmethod
    def list_tables(self, schema_name: str) -> list[str]:
        """列出指定 Schema 下的表。"""

    @abstractmethod
    def get_table_metadata(self, schema_name: str, table_name: str) -> TableMetadata:
        """返回数据库方言无关的表元数据。"""

    @abstractmethod
    def sample_table(
        self,
        schema_name: str,
        table_name: str,
        column_names: Sequence[str],
        limit: int,
        order_by: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """按预算采样表数据；实现必须限制返回行数并安全处理标识符。"""

    @abstractmethod
    def execute_readonly(
        self, sql: str, parameters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """只读执行；实现必须接入 SQL 安全检查、超时和行数限制。"""

    def close(self) -> None:
        """释放连接池等资源；默认无操作，具体适配器按需覆盖。"""


# 组装根用该工厂按连接参数创建 Connector；服务层不感知具体适配器类。
# 参数键：dialect / host / port / database / username / password(SecretStr)，
# 可选 charset / connect_timeout_seconds / read_timeout_seconds；
# 可选 statement_counter（M3 验证运行注入：适配器逐 SQL 语句计数，含
# Inspector 内部语句与失败尝试；不注入时行为与历史一致）。
ConnectorFactory = Callable[[dict[str, Any]], DataSourceConnector]


# -- M3 关系验证探查端口 --------------------------------------------------------
# 专用只读证据采集端口：请求是类型化 Spec（标识符来自快照元数据白名单，
# 上限为受校验整数），实现方据此构造系统聚合查询——不是任意 SQL 执行入口，
# 不接收业务参数值，不返回原始业务行，只返回聚合计数。


@dataclass(frozen=True)
class WindowMetricsRequest:
    """单侧有界窗口统计：窗口内行数/非空/空串/组合去重（评估唯一性与空值影响）。

    limit 只界定窗口（ORDER BY 主键序 LIMIT；无主键时无序 LIMIT，口径标记
    window_unordered）；不据此声称扫描量受控——查询是否扫描更多行由实现方的
    成本门槛与服务层预算决定。
    """

    schema_name: str
    table_name: str
    columns: tuple[str, ...]
    order_by: tuple[str, ...]
    limit: int
    string_columns: tuple[str, ...]  # 需要统计空串的列（其余类型跳过空串口径）
    timeout_ms: int  # 数据库级执行期限提示（支持时生效；范围见实现方文档）


@dataclass(frozen=True)
class JoinMetricsRequest:
    """有界左窗 JOIN 统计：覆盖率、匹配/未匹配、INNER 行数与右端去重匹配数。

    typed 等值比较（ON left.c = right.c），不做隐式类型转换制造匹配率；
    NULL 键不参与匹配；空串是否计入"有意义匹配"由服务层按 empty 统计扣除。

    右端匹配规模控制（修复轮）：默认右端为**完整表**——仅当服务层成本门槛
    判定安全（右端键唯一且有可用前导索引，或无索引但行数估计在小量级余量
    内，每左行 ≤1 次匹配）时使用；right_limit 给定时右端改为**有界窗口
    派生表**（双侧窗口各 ≤W 行，中间结果 ≤W×W 对，用于右端键非唯一等
    展开量不可控的场景；口径降级为样本，由服务层标记）。
    """

    left_schema: str
    left_table: str
    left_columns: tuple[str, ...]
    left_order_by: tuple[str, ...]
    left_limit: int
    right_schema: str
    right_table: str
    right_columns: tuple[str, ...]
    # 右端"匹配存在"判定列（主键优先；保证非空，用于 LEFT JOIN 匹配标记）
    right_presence_column: str
    string_join_columns: tuple[str, ...]  # 连接键中的字符串列（统计空键匹配）
    timeout_ms: int
    # 右端有界窗口（None=完整表，由成本门槛决定）；给定时右端为派生表
    right_order_by: tuple[str, ...] = ()
    right_limit: int | None = None


@dataclass(frozen=True)
class WindowMetricsResult:
    window_rows: int
    non_null_rows: int  # 指定列全部非 NULL 的行数
    empty_string_rows: int  # 非 NULL 但字符串列存在空串的行数
    distinct_values: int  # 非空组合去重数（窗口内）
    truncated: bool = False  # 窗口是否达到 limit（未覆盖整表）


@dataclass(frozen=True)
class JoinMetricsResult:
    left_rows_seen: int | None  # 左窗行数（有主键时精确；无主键为 None）
    unmatched_left_rows: int  # 左窗中未匹配任何右行的行数（含 NULL 键行）
    rows_after_left_join: int  # LEFT JOIN 后总行数（= 未匹配行 + INNER 行数）
    distinct_right_matched: int  # 被匹配到的右端组合去重数
    empty_key_matched_rows: int = 0  # 匹配但左键含空串的行数（不视为有意义匹配）


class RelationshipProbe(ABC):
    """关系证据只读探查端口。实现必须：

    - 仅执行由 Spec 构造的系统聚合查询（标识符白名单转义、LIMIT 为整数）；
    - 连接级只读（复用 DataSourceConnector 的只读策略）；
    - 支持时应用数据库级执行期限（并如实声明支持范围）；
    - 失败抛 DataSourceConnectionError（已脱敏），不携带业务值或 SQL 文本。
    """

    @abstractmethod
    def window_metrics(self, request: WindowMetricsRequest) -> WindowMetricsResult:
        """窗口统计；实现自行限制返回聚合行数（恒为 1 行）。"""

    @abstractmethod
    def join_metrics(self, request: JoinMetricsRequest) -> JoinMetricsResult:
        """左窗 JOIN 统计；右端探测成本由实现/服务层成本门槛控制。"""

    def close(self) -> None:
        """释放资源；默认无操作。"""


ProbeFactory = Callable[[dict[str, Any]], RelationshipProbe]

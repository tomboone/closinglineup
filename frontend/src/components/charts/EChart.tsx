import { useEffect, useRef } from "react";
import type { CSSProperties } from "react";
import { init } from "./echarts";
import type { ChartOption } from "./echarts";

type ECharts = ReturnType<typeof init>;

/**
 * Mounts an ECharts instance on a div and keeps it showing `option`, replacing (not merging) the
 * previous one on every change. The div needs a size from `className` or `style`.
 */
function EChart({
  option,
  className,
  style,
}: {
  option: ChartOption;
  className?: string;
  style?: CSSProperties;
}) {
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<ECharts | null>(null);

  useEffect(() => {
    const element = container.current!;
    const instance = init(element);
    chart.current = instance;
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(element);
    return () => {
      observer.disconnect();
      instance.dispose();
      chart.current = null;
    };
  }, []);

  useEffect(() => {
    chart.current?.setOption(option, { notMerge: true });
  }, [option]);

  return <div ref={container} className={className} style={style} />;
}

export default EChart;

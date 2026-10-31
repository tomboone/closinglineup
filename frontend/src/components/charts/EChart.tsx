import type { ECElementEvent } from "echarts";
import { useEffect, useRef } from "react";
import type { CSSProperties } from "react";
import { init } from "./echarts";
import type { ChartOption } from "./echarts";

type ECharts = ReturnType<typeof init>;

/**
 * Mounts an ECharts instance on a div and keeps it showing `option`, replacing (not merging) the
 * previous one on every change, and calls `onClick` with a clicked mark's event. The div needs a
 * size from `className` or `style`.
 */
function EChart({
  option,
  onClick,
  className,
  style,
}: {
  option: ChartOption;
  onClick?: (event: ECElementEvent) => void;
  className?: string;
  style?: CSSProperties;
}) {
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<ECharts | null>(null);
  // Read through a ref, so the handler bound once at mount always calls the latest `onClick`.
  const click = useRef(onClick);

  useEffect(() => {
    click.current = onClick;
  }, [onClick]);

  useEffect(() => {
    const element = container.current!;
    const instance = init(element);
    chart.current = instance;
    instance.on("click", (event) => click.current?.(event));
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

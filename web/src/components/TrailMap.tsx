import L from "leaflet";
import { useEffect, useState } from "react";
import { GeoJSON, MapContainer, Marker, Popup, TileLayer, useMap } from "react-leaflet";
import { api } from "../api/client";
import type { RoutePlan, TrailFeature } from "../api/types";
import type { VetoedTrail } from "../hooks/useChat";

const CHOSEN_STYLE = { color: "#2f5d46", weight: 5, opacity: 0.9 };
const VETO_STYLE = { color: "#b3412f", weight: 3, dashArray: "6 6", opacity: 0.8 };

const stopIcon = (n: number) =>
  L.divIcon({ className: "", html: `<div class="stop-pin">${n}</div>`, iconSize: [24, 24] });

/** Loads a trail's GeoJSON from the API (null until loaded or if missing). */
function useTrail(trailId: string | undefined): TrailFeature | null {
  const [feature, setFeature] = useState<TrailFeature | null>(null);
  useEffect(() => {
    let alive = true;
    setFeature(null);
    if (trailId) void api.trail(trailId).then((f) => alive && setFeature(f));
    return () => {
      alive = false;
    };
  }, [trailId]);
  return feature;
}

/** Zooms the map to the chosen trail and its stops whenever the plan changes. */
function FitToPlan({ plan, feature }: { plan: RoutePlan | null; feature: TrailFeature | null }) {
  const map = useMap();
  useEffect(() => {
    if (!plan) return;
    const bounds = L.latLngBounds(plan.stops.map((s) => s.location));
    if (feature) bounds.extend(L.geoJSON(feature).getBounds());
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [40, 40] });
  }, [map, plan, feature]);
  return null;
}

function VetoedLayer({ trail }: { trail: VetoedTrail }) {
  const feature = useTrail(trail.trailId);
  if (!feature) return null;
  return (
    <GeoJSON data={feature} style={VETO_STYLE}>
      <Popup>
        <b>{feature.properties.name_he || feature.properties.name}</b>
        {trail.reasons.map((r) => (
          <div key={r}>{r}</div>
        ))}
      </Popup>
    </GeoJSON>
  );
}

export function TrailMap({ plan, vetoed }: { plan: RoutePlan | null; vetoed: VetoedTrail[] }) {
  const feature = useTrail(plan?.trail_id);
  return (
    <section className="map-wrap">
      <MapContainer center={[33.0, 35.5]} zoom={10} className="map">
        <TileLayer
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
          attribution="&copy; OpenStreetMap contributors"
          maxZoom={18}
        />
        {vetoed.map((v) => (
          <VetoedLayer key={v.trailId} trail={v} />
        ))}
        {/* key forces a fresh layer when the trail changes (GeoJSON data is not reactive) */}
        {feature && <GeoJSON key={feature.properties.trail_id} data={feature} style={CHOSEN_STYLE} />}
        {plan?.stops.map((s, i) => (
          <Marker key={`${plan.plan_id}-${i}`} position={s.location} icon={stopIcon(i + 1)}>
            <Popup>
              <b>{s.name}</b>
              <div>{s.arrive_at?.slice(0, 5)}</div>
              {s.note && <div>{s.note}</div>}
            </Popup>
          </Marker>
        ))}
        <FitToPlan plan={plan} feature={feature} />
      </MapContainer>
      <div className="legend">
        <span>
          <i className="sw chosen" />
          מסלול נבחר
        </span>
        <span>
          <i className="sw vetoed" />
          נפסל בבדיקת בטיחות
        </span>
      </div>
    </section>
  );
}

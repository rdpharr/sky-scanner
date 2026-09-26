/* Optional per-message JSONL output for the sky-scanner collector.
 * Include after readsb.h; event fields come only from this decoded message.
 */
#ifndef SKY_SCANNER_READSB_EVENT_H
#define SKY_SCANNER_READSB_EVENT_H

#include <math.h>
#include <stdio.h>
#include <stdlib.h>

static inline const char *sky_addr_type(addrtype_t type) {
    switch (type) {
    case ADDR_ADSB_ICAO: return "icao";
    case ADDR_ADSB_ICAO_NT: return "icao_nontransponder";
    case ADDR_ADSR_ICAO: return "adsr_icao";
    case ADDR_TISB_ICAO: return "tisb_icao";
    case ADDR_JAERO: return "jaero";
    case ADDR_MLAT: return "mlat";
    case ADDR_OTHER: return "other";
    case ADDR_MODE_S: return "mode_s";
    case ADDR_ADSB_OTHER: return "adsb_other";
    case ADDR_ADSR_OTHER: return "adsr_other";
    case ADDR_TISB_TRACKFILE: return "tisb_trackfile";
    case ADDR_TISB_OTHER: return "tisb_other";
    case ADDR_MODE_A: return "mode_a";
    case ADDR_UNKNOWN: return "unknown";
    default: return "unknown";
    }
}

static inline const char *sky_source(datasource_t source) {
    switch (source) {
    case SOURCE_MODE_AC: return "mode_ac";
    case SOURCE_SBS: return "sbs";
    case SOURCE_MLAT: return "mlat";
    case SOURCE_MODE_S: return "mode_s";
    case SOURCE_JAERO: return "jaero";
    case SOURCE_MODE_S_CHECKED: return "mode_s_checked";
    case SOURCE_TISB: return "tisb";
    case SOURCE_ADSR: return "adsr";
    case SOURCE_NT: return "adsb_nontransponder";
    case SOURCE_ADSB: return "adsb";
    case SOURCE_PRIO: return "priority_input";
    case SOURCE_INDIRECT: return "indirect";
    default: return "invalid";
    }
}

static inline const char *sky_origin(addrtype_t type) {
    switch (type) {
    case ADDR_ADSR_ICAO:
    case ADDR_TISB_ICAO:
    case ADDR_ADSR_OTHER:
    case ADDR_TISB_TRACKFILE:
    case ADDR_TISB_OTHER:
        return "rebroadcast";
    case ADDR_ADSB_ICAO_NT:
    case ADDR_ADSB_OTHER:
    case ADDR_MODE_S:
    case ADDR_MODE_A:
    case ADDR_ADSB_ICAO:
        return "direct";
    default:
        return "unknown";
    }
}

static inline void sky_json_string(FILE *out, const char *s) {
    const unsigned char *p = (const unsigned char *)(s ? s : "");
    fputc('"', out);
    for (; *p; ++p) {
        switch (*p) {
        case '"': fputs("\\\"", out); break;
        case '\\': fputs("\\\\", out); break;
        case '\b': fputs("\\b", out); break;
        case '\f': fputs("\\f", out); break;
        case '\n': fputs("\\n", out); break;
        case '\r': fputs("\\r", out); break;
        case '\t': fputs("\\t", out); break;
        default:
            if (*p < 0x20) fprintf(out, "\\u%04x", (unsigned)*p);
            else fputc(*p, out);
        }
    }
    fputc('"', out);
}

static inline void sky_emit(const struct modesMessage *mm) {
    static int enabled = -1;
    if (enabled < 0) enabled = getenv("SKY_SCANNER_JSON") != NULL;
    if (!enabled || !mm || mm->decodeResult < 0 || mm->msgtype == DFTYPE_MODEAC || mm->addr == HEX_UNKNOWN)
        return;

    FILE *out = stdout;
    flockfile(out);
    fprintf(out, "{\"received_at\":%.3f,\"address\":\"%06x\",\"address_type\":\"%s\",\"address_non_icao\":%s,\"origin\":\"%s\",\"source\":\"%s\",\"message_type\":%d,\"message_bits\":%d,\"corrected_bits\":%d,\"signal_level\":%.6g",
            (double)mm->sysTimestamp / 1000.0, mm->addr & 0xffffff,
            sky_addr_type(mm->addrtype), (mm->addr & MODES_NON_ICAO_ADDRESS) ? "true" : "false",
            sky_origin(mm->addrtype), sky_source(mm->source), mm->msgtype,
            mm->msgbits, mm->correctedbits, mm->signalLevel);

#define SKY_INT(key, valid, value) do { if (valid) fprintf(out, ",\"%s\":%d", key, (int)(value)); } while (0)
#define SKY_UINT(key, valid, value) do { if (valid) fprintf(out, ",\"%s\":%u", key, (unsigned)(value)); } while (0)
#define SKY_NUM(key, valid, value) do { double sky_v_ = (double)(value); if ((valid) && isfinite(sky_v_)) fprintf(out, ",\"%s\":%.9g", key, sky_v_); } while (0)
#define SKY_BOOL(key, valid, value) do { if (valid) fprintf(out, ",\"%s\":%s", key, (value) ? "true" : "false"); } while (0)

    if (mm->callsign_valid) {
        fputs(",\"callsign\":", out);
        sky_json_string(out, mm->callsign);
    }
    if (mm->baro_alt_valid) {
        if (mm->baro_alt_unit == UNIT_METERS) SKY_INT("altitude_baro_m", 1, mm->baro_alt);
        else SKY_INT("altitude_baro_ft", 1, mm->baro_alt);
    }
    if (mm->geom_alt_valid) {
        if (mm->geom_alt_unit == UNIT_METERS) SKY_INT("altitude_geom_m", 1, mm->geom_alt);
        else SKY_INT("altitude_geom_ft", 1, mm->geom_alt);
    }
    SKY_NUM("ground_speed_kt", mm->gs_valid, mm->gs.selected);
    if (mm->heading_valid || mm->track_valid) {
        const char *heading_type = "unknown";
        switch (mm->heading_type) {
        case HEADING_GROUND_TRACK: heading_type = "ground_track"; break;
        case HEADING_TRUE: heading_type = "true_heading"; break;
        case HEADING_MAGNETIC: heading_type = "magnetic_heading"; break;
        case HEADING_MAGNETIC_OR_TRUE: heading_type = "magnetic_or_true_heading"; break;
        case HEADING_TRACK_OR_HEADING: heading_type = "track_or_heading"; break;
        default: break;
        }
        fprintf(out, ",\"%s\":%.9g,\"heading_type\":\"%s\"",
                mm->heading_type == HEADING_GROUND_TRACK ? "track_deg" : "heading_deg",
                mm->heading, heading_type);
    }
    SKY_INT("vertical_rate_baro_fpm", mm->baro_rate_valid, mm->baro_rate);
    SKY_INT("vertical_rate_geom_fpm", mm->geom_rate_valid, mm->geom_rate);
    SKY_UINT("indicated_airspeed_knots", mm->ias_valid, mm->ias);
    SKY_UINT("true_airspeed_knots", mm->tas_valid, mm->tas);
    SKY_NUM("mach", mm->mach_valid, mm->mach);
    SKY_UINT("squawk", mm->squawk_valid, mm->squawkDec);
    SKY_BOOL("on_ground", mm->airground != AG_INVALID, mm->airground == AG_GROUND);
    SKY_BOOL("alert", mm->alert_valid, mm->alert);
    SKY_BOOL("emergency", mm->emergency_valid, mm->emergency != EMERGENCY_NONE);
    SKY_UINT("category", mm->category_valid, mm->category);
    if (mm->cpr_valid) {
        fprintf(out, ",\"cpr\":{\"odd\":%s,\"surface\":%s,\"latitude\":%u,\"longitude\":%u,\"nucp\":%u}",
                mm->cpr_odd ? "true" : "false",
                mm->cpr_type == CPR_SURFACE ? "true" : "false",
                mm->cpr_lat, mm->cpr_lon, mm->cpr_nucp);
    }
    SKY_NUM("latitude", mm->cpr_decoded, mm->decoded_lat);
    SKY_NUM("longitude", mm->cpr_decoded, mm->decoded_lon);
    SKY_UINT("nic", mm->cpr_decoded, mm->decoded_nic);
    SKY_UINT("containment_radius_m", mm->cpr_decoded, mm->decoded_rc);
    SKY_INT("geom_minus_baro_altitude", mm->geom_delta_valid, mm->geom_delta);
    SKY_NUM("roll_deg", mm->roll_valid, mm->roll);
    SKY_NUM("track_rate_deg_s", mm->track_rate_valid, mm->track_rate);
    SKY_NUM("wind_speed", mm->wind_valid, mm->wind_speed);
    SKY_NUM("wind_direction_deg", mm->wind_valid, mm->wind_direction);
    SKY_NUM("outside_air_temperature_c", mm->oat_valid, mm->oat);
    SKY_INT("static_pressure", mm->static_pressure_valid, mm->static_pressure);
    SKY_INT("turbulence", mm->turbulence_valid, mm->turbulence);
    SKY_NUM("humidity_percent", mm->humidity_valid, mm->humidity);
    SKY_INT("meteorological_source", mm->met_source_valid, mm->met_source);
    SKY_BOOL("spi", mm->spi_valid, mm->spi);
    SKY_BOOL("squawk_emergency", mm->squawk_emergency_valid, mm->squawk_emergency);
    SKY_NUM("fms_selected_altitude_ft", mm->nav.fms_altitude_valid, mm->nav.fms_altitude);
    SKY_NUM("mcp_selected_altitude_ft", mm->nav.mcp_altitude_valid, mm->nav.mcp_altitude);
    SKY_NUM("qnh_millibars", mm->nav.qnh_valid, mm->nav.qnh);
    SKY_BOOL("position_bad", 1, mm->pos_bad);
    SKY_BOOL("position_old", 1, mm->pos_old);
    SKY_BOOL("garbage", 1, mm->garbage);
    if (mm->accuracy.nic_a_valid) fprintf(out, ",\"nic_a\":%s", mm->accuracy.nic_a ? "true" : "false");
    if (mm->accuracy.nic_b_valid) fprintf(out, ",\"nic_b\":%s", mm->accuracy.nic_b ? "true" : "false");
    if (mm->accuracy.nic_c_valid) fprintf(out, ",\"nic_c\":%s", mm->accuracy.nic_c ? "true" : "false");
    SKY_UINT("nac_p", mm->accuracy.nac_p_valid, mm->accuracy.nac_p);
    SKY_UINT("nac_v", mm->accuracy.nac_v_valid, mm->accuracy.nac_v);
    SKY_UINT("sil", mm->accuracy.sil_type != SIL_INVALID, mm->accuracy.sil);
    SKY_UINT("gva", mm->accuracy.gva_valid, mm->accuracy.gva);
    SKY_UINT("sda", mm->accuracy.sda_valid, mm->accuracy.sda);
    if (mm->opstatus.valid)
        fprintf(out, ",\"adsb_version\":%u,\"operational_status_valid\":true", mm->opstatus.version);
#undef SKY_INT
#undef SKY_UINT
#undef SKY_NUM
#undef SKY_BOOL
    fputs("}\n", out);
    fflush(out);
    funlockfile(out);
}

#endif

import SwiftUI

struct CallView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var voice: VoiceCoordinator
    @EnvironmentObject private var settings: AppSettingsStore

    var body: some View {
        VStack(spacing: 16) {
            statusCard
            controls
            transcriptList
        }
        .padding()
        .background(Color(.systemGroupedBackground).ignoresSafeArea())
    }

    private var statusCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Circle()
                    .fill(phaseColor)
                    .frame(width: 12, height: 12)
                Text(voice.status.label)
                    .font(.title3.weight(.semibold))
                Spacer()
                Text(voice.routeSummary)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            if voice.status.detail.isEmpty == false {
                Text(voice.status.detail)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            if let refreshError = model.refreshError, voice.isRunning == false {
                Text(refreshError)
                    .font(.footnote)
                    .foregroundStyle(.red)
            }
            HStack {
                Label(model.voiceConfigured ? "Voice ready" : "Voice not configured", systemImage: model.voiceConfigured ? "checkmark.circle" : "exclamationmark.triangle")
                    .font(.footnote)
                    .foregroundStyle(model.voiceConfigured ? Color.secondary : Color.orange)
                Spacer()
                if model.agentName.isEmpty == false {
                    Text(model.agentName)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding()
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.secondarySystemBackground)))
    }

    private var controls: some View {
        VStack(spacing: 12) {
            HStack(spacing: 12) {
                sessionPicker
                voicePicker
            }

            HStack(spacing: 12) {
                Button {
                    Task { await model.refresh() }
                } label: {
                    Label(model.isRefreshing ? "Refreshing" : "Refresh", systemImage: "arrow.clockwise")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .disabled(voice.isRunning || model.isRefreshing)

                Button {
                    if voice.isRunning {
                        voice.stop()
                    } else {
                        Task { await model.startCall() }
                    }
                } label: {
                    Label(voice.isRunning ? "End" : "Start call", systemImage: voice.isRunning ? "stop.fill" : "phone.fill")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .tint(voice.isRunning ? .red : .accentColor)
                .disabled(voice.isRunning == false && canStart == false)
            }

            HStack(spacing: 12) {
                Button {
                    voice.setMuted(!voice.isMuted)
                } label: {
                    Label(voice.isMuted ? "Unmute" : "Mute", systemImage: voice.isMuted ? "mic.slash.fill" : "mic.fill")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .disabled(voice.isRunning == false)

                Button {
                    let next = !settings.preferSpeaker
                    settings.preferSpeaker = next
                    voice.setPreferSpeaker(next)
                } label: {
                    Label(settings.preferSpeaker ? "Speaker" : "Headphones / receiver", systemImage: settings.preferSpeaker ? "speaker.wave.2.fill" : "headphones")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
            }

            Text("Uses a system CallKit outgoing call. AirPods single press mutes or unmutes the mic (playback keeps going). UI Mute uses the same call state. AirPods take route priority. Start unlocked, then keep talking with the screen locked.")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var sessionPicker: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Session")
                .font(.caption)
                .foregroundStyle(.secondary)
            Picker("Session", selection: $settings.selectedSlug) {
                if model.liveSessions.isEmpty {
                    Text("No live sessions").tag("")
                }
                ForEach(model.liveSessions) { session in
                    Text(session.title.isEmpty ? session.slug : session.title)
                        .tag(session.slug)
                }
            }
            .disabled(voice.isRunning)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var voicePicker: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Voice")
                .font(.caption)
                .foregroundStyle(.secondary)
            Picker("Voice", selection: $settings.selectedVoice) {
                if model.voices.isEmpty {
                    Text("Default").tag("")
                }
                ForEach(model.voices, id: \.self) { name in
                    Text(name).tag(name)
                }
            }
            .disabled(voice.isRunning || model.voices.count < 2)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var transcriptList: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Transcript")
                .font(.headline)
            if voice.transcript.isEmpty {
                Text("Ephemeral lines appear here during a call.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 120, alignment: .topLeading)
                    .padding()
                    .background(RoundedRectangle(cornerRadius: 12).fill(Color(.secondarySystemBackground)))
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 8) {
                            ForEach(voice.transcript) { line in
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(line.role == "user" ? "You" : "Assistant")
                                        .font(.caption.weight(.semibold))
                                        .foregroundStyle(.secondary)
                                    Text(line.text)
                                        .font(.body)
                                        .opacity(line.isFinal ? 1 : 0.75)
                                }
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .id(line.id)
                            }
                        }
                        .padding()
                    }
                    .defaultScrollAnchor(.bottom)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                    .background(RoundedRectangle(cornerRadius: 12).fill(Color(.secondarySystemBackground)))
                    .onChange(of: voice.transcript.count) { _, _ in
                        if let last = voice.transcript.last {
                            proxy.scrollTo(last.id, anchor: .bottom)
                        }
                    }
                }
            }
        }
        .frame(maxHeight: .infinity, alignment: .top)
    }

    private var canStart: Bool {
        if settings.serverURLText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return false
        }
        if settings.selectedSlug.isEmpty {
            return false
        }
        if model.voiceConfigured == false {
            return false
        }
        return true
    }

    private var phaseColor: Color {
        switch voice.status.phase {
        case .idle:
            return .gray
        case .starting, .connecting, .ending:
            return .orange
        case .listening:
            return .green
        case .speaking:
            return .blue
        case .muted:
            return .yellow
        case .error:
            return .red
        }
    }
}

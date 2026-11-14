let activeSchedulePanel = null;
let schedulePanelTrigger = null;

document.addEventListener("DOMContentLoaded", () => {
  for (const modal of document.querySelectorAll("[data-auto-message-modal]")) {
    if (typeof modal.showModal === "function") {
      modal.showModal();
    } else {
      modal.setAttribute("open", "");
    }
  }
  const participantsModal = document.querySelector("[data-auto-event-participants]");
  if (participantsModal && typeof participantsModal.showModal === "function") {
    participantsModal.showModal();
  }
});

document.addEventListener("click", (event) => {
  const openParticipantsButton = event.target.closest("[data-open-event-participants]");
  if (openParticipantsButton) {
    document.querySelector("[data-event-participants-modal]")?.showModal();
    return;
  }

  const closeParticipantsButton = event.target.closest("[data-close-event-participants]");
  if (closeParticipantsButton) {
    closeParticipantsButton.closest("[data-event-participants-modal]")?.close();
    return;
  }

  const participantsModal = event.target.closest("[data-event-participants-modal]");
  if (participantsModal && event.target === participantsModal) {
    participantsModal.close();
    return;
  }

  const openGeneratorButton = event.target.closest("[data-open-slot-generator]");
  if (openGeneratorButton) {
    document.querySelector("[data-slot-generator-modal]")?.showModal();
    return;
  }

  const closeGeneratorButton = event.target.closest("[data-close-slot-generator]");
  if (closeGeneratorButton) {
    closeGeneratorButton.closest("[data-slot-generator-modal]")?.close();
    return;
  }

  const generatorModal = event.target.closest("[data-slot-generator-modal]");
  if (generatorModal && event.target === generatorModal) {
    generatorModal.close();
    return;
  }

  const rejectionButton = event.target.closest("[data-open-rejection-modal]");
  if (rejectionButton) {
    const modal = document.querySelector("[data-rejection-modal]");
    const form = modal?.querySelector("form");
    if (!modal || !form) return;
    form.action = `/interveners/bookings/${rejectionButton.dataset.bookingId}/reject`;
    const formValues = {
      participant_id: rejectionButton.dataset.intervenerId || rejectionButton.dataset.participantId,
      return_to_participant: rejectionButton.dataset.returnToParticipant,
      event_id: rejectionButton.dataset.eventId,
      selected_date: rejectionButton.dataset.selectedDate,
      schedule_scope: rejectionButton.dataset.scheduleScope,
      dashboard_event_id: rejectionButton.dataset.dashboardEventId,
    };
    for (const [field, value] of Object.entries(formValues)) {
      const input = form.querySelector(`[name="${field}"]`);
      if (input) input.value = value || "";
    }
    form.querySelector('[name="reason"]').value = "";
    modal.showModal();
    modal.querySelector('[name="reason"]').focus();
    return;
  }

  const closeRejectionButton = event.target.closest("[data-close-rejection-modal]");
  if (closeRejectionButton) {
    closeRejectionButton.closest("[data-rejection-modal]")?.close();
    return;
  }

  const intervenerButton = event.target.closest("[data-open-intervener-profile]");
  if (intervenerButton) {
    const matrix = intervenerButton.closest("[data-schedule-matrix]");
    const profilesElement = matrix?.querySelector("[data-schedule-interveners]");
    const panel = matrix?.querySelector("[data-schedule-panel]");
    if (!matrix || !profilesElement || !panel) return;

    const profiles = JSON.parse(profilesElement.textContent || "[]");
    const intervener = profiles.find(
      (profile) => String(profile.id) === intervenerButton.dataset.openIntervenerProfile,
    );
    if (!intervener) return;

    panel.querySelector("[data-panel-title]").textContent = `${intervener.emoji} ${intervener.name}`;
    panel.querySelector("[data-panel-time]").textContent = intervener.participant_type;
    const bookings = panel.querySelector("[data-panel-bookings]");
    bookings.replaceChildren();

    const profile = document.createElement("li");
    profile.className = "grid gap-4 rounded-lg border border-slate-200 bg-slate-50 p-4 text-sm";
    const identity = document.createElement("div");
    identity.className = "flex items-start gap-3";
    if (intervener.avatar_url) {
      const avatar = document.createElement("img");
      avatar.src = intervener.avatar_url;
      avatar.alt = intervener.name;
      avatar.className = "h-16 w-16 shrink-0 rounded-xl object-cover";
      identity.append(avatar);
    }

    const details = document.createElement("div");
    details.className = "min-w-0";
    const name = document.createElement("p");
    name.className = "font-semibold text-slate-900";
    name.textContent = `${intervener.emoji} ${intervener.name}`;
    details.append(name);
    const type = document.createElement("p");
    type.className = "mt-1 text-xs text-slate-500";
    type.textContent = intervener.participant_type;
    details.append(type);
    if (intervener.bio) {
      const bio = document.createElement("p");
      bio.className = "mt-2 whitespace-pre-line text-slate-700";
      bio.textContent = intervener.bio;
      details.append(bio);
    }
    if (intervener.social_url) {
      const social = document.createElement("a");
      social.href = intervener.social_url;
      social.target = "_blank";
      social.rel = "noopener noreferrer";
      social.className = "mt-2 inline-block break-all text-indigo-700 hover:underline";
      social.textContent = matrix.dataset.socialLabel;
      details.append(social);
    }
    identity.append(details);
    profile.append(identity);

    if (intervener.photos?.length) {
      const gallery = document.createElement("div");
      gallery.className = "grid gap-2";
      const galleryTitle = document.createElement("span");
      galleryTitle.className = "text-xs font-semibold text-slate-600";
      galleryTitle.textContent = matrix.dataset.profilePhotosLabel;
      const images = document.createElement("div");
      images.className = "flex flex-wrap gap-2";
      for (const photoUrl of intervener.photos) {
        const link = document.createElement("a");
        link.href = photoUrl;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        const image = document.createElement("img");
        image.src = photoUrl;
        image.alt = `${intervener.name} — ${matrix.dataset.profilePhotosLabel}`;
        image.loading = "lazy";
        image.className = "h-20 w-20 rounded-lg object-cover";
        link.append(image);
        images.append(link);
      }
      gallery.append(galleryTitle, images);
      profile.append(gallery);
    }
    bookings.append(profile);
    panel.querySelector("[data-panel-queue]").textContent = "";
    panel.querySelectorAll(
      "[data-panel-admin-form], [data-panel-create-form], [data-panel-intervener-form], [data-panel-delete-form], [data-panel-book-form], [data-panel-cancel-form]",
    ).forEach((form) => form.classList.add("hidden"));

    schedulePanelTrigger = intervenerButton;
    activeSchedulePanel = panel;
    panel.classList.remove("hidden");
    panel.setAttribute("aria-hidden", "false");
    panel.querySelector("[data-close-schedule-panel]")?.focus();
    return;
  }

  const createSlotButton = event.target.closest("[data-open-create-slot]");
  if (createSlotButton) {
    const matrix = document.querySelector("[data-schedule-matrix]");
    const panel = matrix?.querySelector("[data-schedule-panel]");
    const createForm = panel?.querySelector("[data-panel-create-form]");
    if (!matrix || !panel || !createForm) return;

    panel.querySelector("[data-panel-title]").textContent = createSlotButton.dataset.panelTitle;
    panel.querySelector("[data-panel-time]").textContent = "";
    panel.querySelector("[data-panel-bookings]").replaceChildren();
    panel.querySelector("[data-panel-queue]").textContent = "";
    createForm.classList.remove("hidden");
    panel.querySelector("[data-panel-admin-form]")?.classList.add("hidden");
    panel.querySelector("[data-panel-intervener-form]")?.classList.add("hidden");
    panel.querySelector("[data-panel-delete-form]")?.classList.add("hidden");

    schedulePanelTrigger = createSlotButton;
    activeSchedulePanel = panel;
    panel.classList.remove("hidden");
    panel.setAttribute("aria-hidden", "false");
    createForm.querySelector('[name="intervener_id"]')?.focus();
    return;
  }

  const slotButton = event.target.closest("[data-open-schedule-slot]");
  if (slotButton) {
    const matrix = slotButton.closest("[data-schedule-matrix]");
    const dataElement = matrix?.querySelector("[data-schedule-slots]");
    const panel = matrix?.querySelector("[data-schedule-panel]");
    if (!matrix || !dataElement || !panel) return;

    const slots = JSON.parse(dataElement.textContent || "[]");
    const slot = slots.find((item) => String(item.id) === slotButton.dataset.openScheduleSlot);
    if (!slot) return;

    const title = panel.querySelector("[data-panel-title]");
    const time = panel.querySelector("[data-panel-time]");
    const bookings = panel.querySelector("[data-panel-bookings]");
    const queue = panel.querySelector("[data-panel-queue]");
    const adminForm = panel.querySelector("[data-panel-admin-form]");
    const intervenerForm = panel.querySelector("[data-panel-intervener-form]");
    const createForm = panel.querySelector("[data-panel-create-form]");
    const deleteForm = panel.querySelector("[data-panel-delete-form]");
    const bookForm = panel.querySelector("[data-panel-book-form]");
    const cancelForm = panel.querySelector("[data-panel-cancel-form]");
    const bookSlotInput = bookForm?.querySelector('[name="slot_id"]');
    const cancelSlotInput = cancelForm?.querySelector('[name="slot_id"]');

    title.textContent = `${slot.intervener_emoji} ${slot.intervener}`;
    time.textContent = `${slot.is_lunch_break ? `${matrix.dataset.lunchLabel} · ` : ""}${slot.display_start}–${slot.display_end}`;
    bookings.replaceChildren();
    if (slot.bookings.length === 0) {
      const empty = document.createElement("li");
      empty.className = "rounded-lg border border-dashed border-slate-300 p-4 text-sm text-slate-500";
      empty.textContent = slot.can_review ? matrix.dataset.noRequestsLabel : matrix.dataset.noBookingsLabel;
      bookings.append(empty);
    } else {
      const pendingCount = slot.bookings.filter((booking) => booking.status_value === "pending").length;
      if (slot.can_review && pendingCount > 0) {
        const priority = document.createElement("li");
        priority.className = "text-xs text-slate-500";
        priority.textContent = matrix.dataset.queuePriorityLabel;
        bookings.append(priority);
      }
      for (const booking of slot.bookings) {
        const item = document.createElement("li");
        item.className = "grid gap-3 rounded-lg border border-slate-200 bg-slate-50 p-3 text-sm";
        const isWaiting = slot.can_review && booking.status_value === "pending" && !booking.can_process;
        if (isWaiting) {
          item.classList.add("opacity-50", "grayscale");
          item.setAttribute("aria-disabled", "true");
        }
        const profile = document.createElement("div");
        profile.className = "flex items-start gap-3";
        if (booking.avatar_url) {
          const avatar = document.createElement("img");
          avatar.src = booking.avatar_url;
          avatar.alt = booking.name;
          avatar.className = "h-14 w-14 shrink-0 rounded-xl object-cover";
          profile.append(avatar);
        } else {
          const avatar = document.createElement("span");
          avatar.className = "grid h-14 w-14 shrink-0 place-items-center rounded-xl bg-indigo-100 text-2xl";
          avatar.setAttribute("aria-hidden", "true");
          avatar.textContent = booking.emoji;
          profile.append(avatar);
        }
        const identity = document.createElement("div");
        identity.className = "min-w-0";
        const name = document.createElement("span");
        name.className = "font-semibold text-slate-900";
        name.textContent = `${booking.emoji} ${booking.name}`;
        identity.append(name);
        if (booking.participant_type) {
          const participantType = document.createElement("span");
          participantType.className = "mt-0.5 block text-xs text-slate-500";
          participantType.textContent = booking.participant_type;
          identity.append(participantType);
        }
        const details = document.createElement("span");
        details.className = "mt-1 block text-slate-600";
        details.textContent = `${booking.status} · ${booking.location}${booking.notes ? ` · ${booking.notes}` : ""}`;
        identity.append(details);
        if (isWaiting) {
          const waiting = document.createElement("span");
          waiting.className = "mt-2 block text-xs font-semibold text-slate-500";
          waiting.textContent = matrix.dataset.queueWaitingLabel;
          identity.append(waiting);
        }
        if (booking.bio) {
          const bio = document.createElement("p");
          bio.className = "mt-2 whitespace-pre-line text-slate-700";
          bio.textContent = booking.bio;
          identity.append(bio);
        }
        if (booking.email) {
          const email = document.createElement("a");
          email.href = `mailto:${booking.email}`;
          email.className = "mt-1 block break-all text-indigo-700 hover:underline";
          email.textContent = `${matrix.dataset.emailLabel}: ${booking.email}`;
          identity.append(email);
        }
        if (booking.discord_handle) {
          const discordHandle = document.createElement("p");
          discordHandle.className = "mt-1 text-slate-600";
          discordHandle.textContent = `Discord: ${booking.discord_handle}`;
          identity.append(discordHandle);
        }
        if (booking.social_url) {
          const social = document.createElement("a");
          social.href = booking.social_url;
          social.target = "_blank";
          social.rel = "noopener noreferrer";
          social.className = "mt-2 inline-block break-all text-indigo-700 hover:underline";
          social.textContent = matrix.dataset.socialLabel;
          identity.append(social);
        }
        profile.append(identity);
        item.append(profile);
        if (booking.profile_photos?.length) {
          const gallery = document.createElement("div");
          gallery.className = "grid gap-2";
          const galleryTitle = document.createElement("span");
          galleryTitle.className = "text-xs font-semibold text-slate-600";
          galleryTitle.textContent = matrix.dataset.profilePhotosLabel;
          const galleryImages = document.createElement("div");
          galleryImages.className = "flex flex-wrap gap-2";
          for (const photoUrl of booking.profile_photos) {
            const link = document.createElement("a");
            link.href = photoUrl;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            const photo = document.createElement("img");
            photo.src = photoUrl;
            photo.alt = `${booking.name} — ${matrix.dataset.profilePhotosLabel}`;
            photo.loading = "lazy";
            photo.className = "h-20 w-20 rounded-lg object-cover";
            link.append(photo);
            galleryImages.append(link);
          }
          gallery.append(galleryTitle, galleryImages);
          item.append(gallery);
        }
        if (booking.photos?.length) {
          const gallery = document.createElement("div");
          gallery.className = "flex flex-wrap gap-2";
          for (const photoUrl of booking.photos) {
            const link = document.createElement("a");
            link.href = photoUrl;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            const photo = document.createElement("img");
            photo.src = photoUrl;
            photo.alt = booking.name;
            photo.className = "h-20 w-20 rounded-lg object-cover";
            link.append(photo);
            gallery.append(link);
          }
          item.append(gallery);
        }
        if (slot.can_review && booking.status_value === "pending") {
          const actions = document.createElement("div");
          actions.className = "flex gap-2";
          for (const [action, label, style] of [
            ["approve", matrix.dataset.approveLabel, "rounded-lg bg-emerald-700 px-3 py-2 text-xs font-semibold text-white"],
          ]) {
            const form = document.createElement("form");
            form.method = "post";
            form.action = `/interveners/bookings/${booking.booking_id}/${action}`;
            for (const [field, value] of [
              ["csrf_token", slot.csrf_token],
              ["participant_id", slot.intervener_id],
              ["return_to_participant", "1"],
              ["event_id", matrix.dataset.eventId],
              ["selected_date", matrix.dataset.selectedDate],
              ["schedule_scope", matrix.dataset.scheduleScope],
            ]) {
              const input = document.createElement("input");
              input.type = "hidden";
              input.name = field;
              input.value = String(value);
              form.append(input);
            }
            const button = document.createElement("button");
            button.type = "submit";
            button.className = style;
            button.textContent = label;
            button.disabled = !booking.can_process;
            if (button.disabled) {
              button.setAttribute("aria-disabled", "true");
              button.classList.add("cursor-not-allowed", "opacity-50");
            }
            form.append(button);
            actions.append(form);
          }
          const rejectButton = document.createElement("button");
          rejectButton.type = "button";
          rejectButton.className = "rounded-lg border px-3 py-2 text-xs font-semibold";
          rejectButton.textContent = matrix.dataset.rejectLabel;
          rejectButton.dataset.openRejectionModal = "";
          rejectButton.dataset.bookingId = booking.booking_id;
          rejectButton.dataset.participantId = slot.intervener_id;
          rejectButton.dataset.returnToParticipant = "1";
          rejectButton.dataset.eventId = matrix.dataset.eventId;
          rejectButton.dataset.selectedDate = matrix.dataset.selectedDate;
          rejectButton.dataset.scheduleScope = matrix.dataset.scheduleScope;
          rejectButton.disabled = !booking.can_reject;
          if (rejectButton.disabled) {
            rejectButton.setAttribute("aria-disabled", "true");
            rejectButton.classList.add("cursor-not-allowed", "opacity-50");
          }
          actions.append(rejectButton);
          item.append(actions);
        }
        bookings.append(item);
      }
    }

    queue.textContent = slot.is_lunch_break || slot.has_approved_booking
      ? ""
      : `${matrix.dataset.refusalsRemainingLabel}: ${slot.remaining_refusals}`;
    if (adminForm) {
      adminForm.classList.remove("hidden");
    }
    if (createForm) {
      createForm.classList.add("hidden");
    }
    if (intervenerForm) {
      intervenerForm.classList.toggle("hidden", !slot.can_edit);
      if (slot.can_edit) {
        intervenerForm.action = `/interveners/time-slots/${slot.id}`;
        intervenerForm.querySelector('[name="date"]').value = slot.date;
        intervenerForm.querySelector('[name="start_time"]').value = slot.start;
        intervenerForm.querySelector('[name="end_time"]').value = slot.end;
        intervenerForm.querySelector('[name="capacity"]').value = String(slot.capacity);
        intervenerForm.querySelector('[name="is_lunch_break"]').checked = slot.is_lunch_break;
      }
    }
    if (adminForm) {
      adminForm.action = `/admin/time-slots/${slot.id}`;
      adminForm.querySelector('[name="date"]').value = slot.date;
      adminForm.querySelector('[name="start_time"]').value = slot.start;
      adminForm.querySelector('[name="end_time"]').value = slot.end;
      adminForm.querySelector('[name="capacity"]').value = String(slot.capacity);
      adminForm.querySelector('[name="is_lunch_break"]').checked = slot.is_lunch_break;
    }
    if (deleteForm) {
      deleteForm.action = `/admin/time-slots/${slot.id}/delete`;
      deleteForm.classList.toggle("hidden", !slot.can_delete);
    }
    if (bookForm) {
      bookForm.classList.toggle("hidden", !slot.can_book);
      if (bookSlotInput) bookSlotInput.value = String(slot.id);
    }
    if (cancelForm) {
      cancelForm.classList.toggle("hidden", !slot.own_booking_status);
      if (cancelSlotInput) cancelSlotInput.value = String(slot.id);
    }

    schedulePanelTrigger = slotButton;
    activeSchedulePanel = panel;
    panel.classList.remove("hidden");
    panel.setAttribute("aria-hidden", "false");
    panel.querySelector("[data-close-schedule-panel]")?.focus();
    return;
  }

  const closeButton = event.target.closest("[data-close-schedule-panel]");
  if (closeButton) {
    const panel = closeButton.closest("[data-schedule-panel]");
    if (!panel) return;
    panel.classList.add("hidden");
    panel.setAttribute("aria-hidden", "true");
    if (activeSchedulePanel === panel) {
      activeSchedulePanel = null;
      schedulePanelTrigger?.focus();
      schedulePanelTrigger = null;
    }
  }
});

document.addEventListener("submit", (event) => {
  const form = event.target.closest("form[data-confirm]");
  if (form && !window.confirm(form.dataset.confirm)) {
    event.preventDefault();
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !activeSchedulePanel) return;
  activeSchedulePanel.classList.add("hidden");
  activeSchedulePanel.setAttribute("aria-hidden", "true");
  activeSchedulePanel = null;
  schedulePanelTrigger?.focus();
  schedulePanelTrigger = null;
});

document.addEventListener("htmx:afterSwap", () => {
  if (activeSchedulePanel?.isConnected) return;
  activeSchedulePanel = null;
  schedulePanelTrigger = null;
});

const showImageLimitModal = (uploader, message) => {
  let modal = document.querySelector("[data-image-limit-modal]");
  if (!modal) {
    modal = document.createElement("dialog");
    modal.setAttribute("data-image-limit-modal", "");
    modal.setAttribute("aria-labelledby", "image-limit-title");
    modal.className = "w-[calc(100%-2rem)] max-w-md rounded-2xl border-0 p-0 shadow-2xl backdrop:bg-slate-950/50";
    modal.innerHTML = `
      <div class="grid gap-4 p-6">
        <div class="flex items-start gap-3">
          <span aria-hidden="true" class="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-amber-100 text-xl">⚠️</span>
          <div class="grid gap-2">
            <h2 id="image-limit-title" data-image-limit-title class="text-lg font-bold text-slate-900"></h2>
            <p data-image-limit-message class="text-sm font-medium text-slate-800"></p>
            <p data-image-limit-hint class="text-sm text-slate-600"></p>
          </div>
        </div>
        <form method="dialog" class="flex justify-end">
          <button data-image-limit-ok class="rounded-xl bg-indigo-700 px-5 py-2.5 text-sm font-semibold text-white hover:bg-indigo-800"></button>
        </form>
      </div>`;
    modal.addEventListener("click", (event) => {
      if (event.target === modal) modal.close();
    });
    document.body.append(modal);
  }
  modal.querySelector("[data-image-limit-title]").textContent = uploader.dataset.limitTitle || "";
  modal.querySelector("[data-image-limit-message]").textContent = message;
  modal.querySelector("[data-image-limit-hint]").textContent = uploader.dataset.limitHint || "";
  modal.querySelector("[data-image-limit-ok]").textContent = uploader.dataset.okLabel || "OK";
  if (typeof modal.showModal === "function") {
    modal.showModal();
  } else {
    window.alert(`${message}\n${uploader.dataset.limitHint || ""}`);
  }
};

for (const uploader of document.querySelectorAll(".image-upload")) {
  const fileInput = uploader.querySelector(".image-upload-input");
  const chooseButton = uploader.querySelector(".image-upload-button");
  const dropZone = uploader.querySelector(".image-drop-zone");
  const status = uploader.querySelector(".image-upload-status");
  const preview = uploader.querySelector(".image-upload-preview");
  const gallery = uploader.querySelector(".image-upload-gallery");
  const target = document.getElementById(uploader.dataset.uploadTarget);
  const maxItems = Number(uploader.dataset.maxItems) || 0;
  let isUploading = false;

  if (!fileInput || !chooseButton || !dropZone || !status || (!preview && !gallery) || !target) {
    continue;
  }

  const currentUrls = () => target.value.split(/\r?\n/).map((url) => url.trim()).filter(Boolean);

  const updateGallery = () => {
    if (!gallery) return;
    gallery.replaceChildren();
    currentUrls().forEach((url, index) => {
      const item = document.createElement("div");
      item.className = "relative";
      const link = document.createElement("a");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      const image = document.createElement("img");
      image.src = url;
      image.alt = `${uploader.dataset.galleryLabel} ${index + 1}`;
      image.className = "h-20 w-20 rounded-lg border object-cover";
      image.loading = "lazy";
      link.append(image);
      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "absolute -right-1.5 -top-1.5 grid h-5 w-5 place-items-center rounded-full bg-rose-600 text-white shadow hover:bg-rose-700";
      removeButton.setAttribute("aria-label", `${uploader.dataset.removeLabel} ${index + 1}`);
      removeButton.innerHTML = '<svg aria-hidden="true" viewBox="0 0 20 20" fill="none" class="h-3 w-3"><path d="M4.5 6h11m-9.5 0 .6 10h5.8l.6-10M8 6V4.5h4V6m-3 3v4m2-4v4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
      removeButton.addEventListener("click", () => {
        target.value = currentUrls().filter((_, itemIndex) => itemIndex !== index).join("\n");
        target.dispatchEvent(new Event("input", { bubbles: true }));
        target.dispatchEvent(new Event("change", { bubbles: true }));
      });
      item.append(link, removeButton);
      gallery.append(item);
    });
  };

  const isLimitReached = () => maxItems > 0 && currentUrls().length >= maxItems;

  const openFilePicker = () => {
    if (isUploading) return;
    if (isLimitReached()) {
      showImageLimitModal(uploader, uploader.dataset.limitReached);
      return;
    }
    fileInput.click();
  };

  const updateUploadControls = () => {
    const limitReached = isLimitReached();
    chooseButton.disabled = isUploading;
    fileInput.disabled = isUploading;
    chooseButton.classList.toggle("opacity-50", limitReached);
    dropZone.setAttribute("aria-disabled", String(isUploading || limitReached));
    dropZone.classList.toggle("pointer-events-none", isUploading);
    dropZone.classList.toggle("opacity-50", isUploading || limitReached);
    if (limitReached && !isUploading) {
      status.textContent = uploader.dataset.limitReached;
      status.classList.remove("text-emerald-700");
      status.classList.add("text-rose-700");
    } else if (status.textContent === uploader.dataset.limitReached) {
      status.textContent = "";
      status.classList.remove("text-rose-700");
    }
  };

  const upload = async (file) => {
    if (!file) return false;
    status.textContent = uploader.dataset.uploading;
    status.classList.remove("text-rose-700", "text-emerald-700");
    const formData = new FormData();
    formData.append("image", file);
    formData.append("csrf_token", uploader.dataset.csrfToken);

    try {
      const response = await fetch("/admin/uploads/images", {
        method: "POST",
        body: formData,
        credentials: "same-origin",
      });
      if (!response.headers.get("content-type")?.includes("application/json")) {
        throw new Error(uploader.dataset.error);
      }
      const result = await response.json();
      if (!response.ok) {
        throw new Error(result.detail || uploader.dataset.error);
      }

      if (uploader.dataset.append === "true") {
        const current = target.value.trim();
        target.value = current ? `${current}\n${result.url}` : result.url;
      } else {
        target.value = result.url;
      }
      target.dispatchEvent(new Event("input", { bubbles: true }));
      target.dispatchEvent(new Event("change", { bubbles: true }));
      if (preview) {
        preview.src = result.url;
        preview.classList.remove("hidden");
      }
      status.textContent = uploader.dataset.success;
      status.classList.add("text-emerald-700");
      return true;
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : uploader.dataset.error;
      status.classList.add("text-rose-700");
      return false;
    }
  };

  const uploadFiles = async (files) => {
    if (!files.length) return;
    const availableItems = maxItems > 0 ? maxItems - currentUrls().length : files.length;
    if (files.length > availableItems) {
      const message = availableItems > 0
        ? (uploader.dataset.limitRemaining || "").replace("{count}", String(availableItems))
        : uploader.dataset.limitReached;
      showImageLimitModal(uploader, message);
      return;
    }
    isUploading = true;
    updateUploadControls();
    let allUploaded = true;
    for (const file of files) {
      allUploaded = await upload(file) && allUploaded;
    }
    isUploading = false;
    updateUploadControls();
    if (!allUploaded) status.classList.add("text-rose-700");
  };

  target.addEventListener("input", () => {
    updateGallery();
    updateUploadControls();
  });
  target.addEventListener("change", () => {
    updateGallery();
    updateUploadControls();
  });
  updateGallery();
  updateUploadControls();

  chooseButton.addEventListener("click", openFilePicker);
  fileInput.addEventListener("change", () => {
    const files = Array.from(fileInput.files || []);
    fileInput.value = "";
    uploadFiles(files);
  });
  dropZone.addEventListener("click", openFilePicker);
  dropZone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openFilePicker();
    }
  });
  dropZone.tabIndex = 0;
  dropZone.setAttribute("role", "button");
  dropZone.addEventListener("dragover", (event) => {
    event.preventDefault();
    dropZone.classList.add("border-indigo-500", "bg-indigo-50");
  });
  dropZone.addEventListener("dragleave", () => {
    dropZone.classList.remove("border-indigo-500", "bg-indigo-50");
  });
  dropZone.addEventListener("drop", (event) => {
    event.preventDefault();
    dropZone.classList.remove("border-indigo-500", "bg-indigo-50");
    uploadFiles(Array.from(event.dataTransfer?.files || []));
  });
}

const menuButton = document.getElementById("menu-button");
const sidebar = document.getElementById("sidebar");

document.querySelectorAll("[data-print-page]").forEach((button) => {
    button.addEventListener("click", () => window.print());
});

document.querySelectorAll("[data-student-picker]").forEach((picker) => {
    const input = picker.querySelector('[role="combobox"]');
    const hidden = picker.querySelector('input[type="hidden"][name="student_id"]');
    const menu = picker.querySelector(".student-picker-menu");
    const list = picker.querySelector('[role="listbox"]');
    const status = picker.querySelector(".student-picker-status");
    const classSelect = picker.dataset.classSelect
        ? document.getElementById(picker.dataset.classSelect)
        : null;
    let timer;
    let controller;
    let activeIndex = -1;

    const closeMenu = () => {
        menu.hidden = true;
        input.setAttribute("aria-expanded", "false");
        input.removeAttribute("aria-activedescendant");
        activeIndex = -1;
    };
    const choose = (option) => {
        hidden.value = option.dataset.studentId;
        input.value = option.dataset.label;
        input.setCustomValidity("");
        status.textContent = `Selected ${option.dataset.label}`;
        closeMenu();
    };
    const setActive = (index) => {
        const options = [...list.querySelectorAll('[role="option"]')];
        if (!options.length) return;
        activeIndex = (index + options.length) % options.length;
        options.forEach((option, optionIndex) => {
            option.setAttribute("aria-selected", String(optionIndex === activeIndex));
        });
        input.setAttribute("aria-activedescendant", options[activeIndex].id);
    };
    const search = async () => {
        const query = input.value.trim();
        hidden.value = "";
        input.setCustomValidity(picker.dataset.optional === "true" || !query
            ? ""
            : "Choose a student from the search results.");
        if (query.length < 2) {
            closeMenu();
            status.textContent = picker.dataset.classSelect && !classSelect?.value
                ? "Select a class, then type at least two characters."
                : picker.dataset.optional === "true"
                    ? "Leave blank to include all students."
                    : "Type at least two characters, then choose a result.";
            return;
        }
        if (classSelect && !classSelect.value) {
            closeMenu();
            status.textContent = "Select a class before searching for students.";
            return;
        }

        controller?.abort();
        controller = new AbortController();
        const params = new URLSearchParams({ q: query });
        if (classSelect?.value) params.set("class_id", classSelect.value);
        if (picker.dataset.enrolledOnly === "true") params.set("enrolled", "1");
        if (picker.dataset.includePast === "true") params.set("include_past", "1");
        status.textContent = "Searching…";
        try {
            const response = await fetch(`/api/student-options?${params}`, {
                headers: { Accept: "application/json" },
                signal: controller.signal,
            });
            if (!response.ok) throw new Error("Search failed");
            const payload = await response.json();
            list.replaceChildren();
            payload.results.forEach((result, index) => {
                const option = document.createElement("button");
                option.type = "button";
                option.id = `${list.id}-option-${index}`;
                option.setAttribute("role", "option");
                option.setAttribute("aria-selected", "false");
                option.dataset.studentId = result.id;
                option.dataset.label = result.label;
                option.textContent = result.label;
                option.addEventListener("click", () => choose(option));
                const item = document.createElement("li");
                item.append(option);
                list.append(item);
            });
            menu.hidden = false;
            input.setAttribute("aria-expanded", "true");
            status.textContent = payload.results.length
                ? `${payload.results.length} matching student${payload.results.length === 1 ? "" : "s"}. Use the arrow keys and Enter to select.`
                : "No matching students found.";
        } catch (error) {
            if (error.name !== "AbortError") status.textContent = "Student search is unavailable. Please try again.";
        }
    };

    input.addEventListener("input", () => {
        hidden.value = "";
        input.setCustomValidity(input.value.trim()
            ? "Choose a student from the search results."
            : "");
        clearTimeout(timer);
        timer = setTimeout(search, 220);
    });
    input.addEventListener("keydown", (event) => {
        if (event.key === "ArrowDown" && !menu.hidden) {
            event.preventDefault();
            setActive(activeIndex + 1);
        } else if (event.key === "ArrowUp" && !menu.hidden) {
            event.preventDefault();
            setActive(activeIndex <= 0 ? list.querySelectorAll('[role="option"]').length - 1 : activeIndex - 1);
        } else if (event.key === "Enter" && !menu.hidden && activeIndex >= 0) {
            event.preventDefault();
            choose(list.querySelectorAll('[role="option"]')[activeIndex]);
        } else if (event.key === "Escape") {
            closeMenu();
        }
    });
    input.addEventListener("blur", () => {
        if (picker.dataset.optional !== "true" && input.value && !hidden.value) {
            input.setCustomValidity("Choose a student from the search results.");
        }
    });
    classSelect?.addEventListener("change", () => {
        controller?.abort();
        input.value = "";
        hidden.value = "";
        input.setCustomValidity("");
        closeMenu();
        status.textContent = "Type at least two characters to search this class.";
    });
    document.addEventListener("click", (event) => {
        if (!picker.contains(event.target)) closeMenu();
    });
});

const classYearSelect = document.querySelector("[data-class-year-select]");
const managedTermSelect = document.querySelector("[data-managed-term-select]");
if (classYearSelect && managedTermSelect) {
    const filterTermsForClass = () => {
        const yearId = classYearSelect.selectedOptions[0]?.dataset.yearId;
        Array.from(managedTermSelect.options).forEach((option) => {
            if (!option.value) return;
            const visible = option.dataset.yearId === yearId;
            option.hidden = !visible;
            option.disabled = !visible;
            if (!visible && option.selected) managedTermSelect.value = "";
        });
    };
    classYearSelect.addEventListener("change", filterTermsForClass);
    filterTermsForClass();
}

if (menuButton && sidebar) {
    const setMenuOpen = (open) => {
        sidebar.classList.toggle("open", open);
        document.body.classList.toggle("nav-open", open);
        menuButton.setAttribute("aria-expanded", String(open));
        menuButton.setAttribute("aria-label", open ? "Close menu" : "Open menu");
    };

    menuButton.addEventListener("click", () => {
        setMenuOpen(!sidebar.classList.contains("open"));
    });
    sidebar.querySelectorAll(".side-nav a").forEach((link) => {
        if (link.classList.contains("active")) link.setAttribute("aria-current", "page");
        link.addEventListener("click", () => setMenuOpen(false));
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") setMenuOpen(false);
    });
    document.addEventListener("click", (event) => {
        if (sidebar.classList.contains("open") && !sidebar.contains(event.target) && !menuButton.contains(event.target)) {
            setMenuOpen(false);
        }
    });
}

document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
        if (!window.confirm(form.dataset.confirm)) {
            event.preventDefault();
        }
    });
});

const editYearDialog = document.getElementById("edit-year-dialog");
const editYearName = document.getElementById("edit-year-name");
let activeEditYearForm = null;

document.querySelectorAll("form.edit-year-form").forEach((form) => {
    const button = form.querySelector(".edit-year-button");
    button.addEventListener("click", (event) => {
        event.preventDefault();
        if (!editYearDialog || !editYearName) return;
        activeEditYearForm = form;
        editYearName.value = form.dataset.name;
        editYearDialog.showModal();
        editYearName.focus();
        editYearName.select();
    });
});

document.getElementById("save-year-edit")?.addEventListener("click", (event) => {
    event.preventDefault();
    const name = editYearName?.value.trim();
    if (!name || !activeEditYearForm) {
        editYearName?.focus();
        return;
    }
    activeEditYearForm.querySelector("input[name='name']").value = name;
    activeEditYearForm.submit();
});

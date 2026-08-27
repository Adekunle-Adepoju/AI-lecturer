from django.db import models
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError

# ── Academic Reference Maps ─────────────────────────────────────────
COURSES = {
    "300": {
        "1": [
            {"code": "CEG211", "title": "Mechanics of Material 1", "units": 3},
            {"code": "CHG331", "title": "Transport Phenomena 2", "units": 3},
            {"code": "GEG311", "title": "Calculus of Several Variables", "units": 3},
            {"code": "GST307", "title": "Entrepreneurship & Corporate Governance", "units": 2},
            {"code": "MEG319", "title": "Mechanical Engineering Technology", "units": 3},
            {"code": "PGG311", "title": "Petroleum Engineering Lab 1", "units": 2},
            {"code": "PGG313", "title": "Basic Petroleum Reservoir Engineering", "units": 3},
            {"code": "PGG314", "title": "Gas Processing Equipments", "units": 3},
            {"code": "PGG317", "title": "Drilling Methods I", "units": 3},
            {"code": "PGG318", "title": "Introduction to Natural Gas Processing", "units": 3},
        ],
        "2": [
            {"code": "CHG341", "title": "Transport Phenomena III", "units": 3},
            {"code": "CHG342", "title": "Separation Processes II", "units": 3},
            {"code": "CHG343", "title": "Thermodynamics I", "units": 3},
            {"code": "GEG322", "title": "Operational Methods I", "units": 3},
            {"code": "PGG321", "title": "Fluid Flow Through Porous Media", "units": 3},
            {"code": "PGG322", "title": "Formation Evaluation and Geophysical Methods", "units": 3},
            {"code": "PGG324", "title": "Drilling Methods II", "units": 3},
            {"code": "PGG325", "title": "PGG Lab II", "units": 2},
            {"code": "PGG327", "title": "Student Work Experience Program", "units": 6},
        ],
    },
    "400": {
        "1": [
            {"code": "GEG411", "title": "Technical Communications", "units": 2},
            {"code": "MME412", "title": "Corrosion & Electrochemistry", "units": 3},
            {"code": "PGG431", "title": "Petroleum Reservoir Engineering", "units": 3},
            {"code": "PGG432", "title": "Petroleum Production Engineering", "units": 3},
            {"code": "PGG433", "title": "Gas Dynamics", "units": 3},
            {"code": "PGG434", "title": "Intro to Well Logging & Interpretation", "units": 3},
            {"code": "PGG435", "title": "Petroleum Engineering Lab III", "units": 2},
            {"code": "PGG436", "title": "Well Testing Methods", "units": 3},
            {"code": "PGG451", "title": "Petroleum Engineering Simulation Tools", "units": 3},
            {"code": "PGG453", "title": "Subsea Engineering", "units": 3},
        ],
        "2": [],
    },
}

COURSE_OUTLINES = {
    "CEG211": {
        1: ["Forces, Moments and Couples", "Resultants and Equivalent Force Systems", "Direct Stresses and Strains"],
        2: ["Hooke's Law and Method of Superposition", "Stresses from Temperature Changes", "Stresses on Thin Cylinders and Spheres"],
        3: ["Stresses on Inclined Planes", "Principal Stresses", "Statically Determinate Body Systems and Plane Pin-Jointed Frames"],
    },
    "CHG331": {
        1: ["Fluid Flow in Pipes and Nozzles", "Flow in Open Channels", "Introduction to Mass Transfer and Fick's Law"],
        2: ["Diffusion in Stationary Media and Additivity of Vapor", "Cooling Tower Design", "Psychrometric Charts"],
        3: ["Estimation of Cooling Tower Weights and Humidifying Towers", "Drying Mechanisms and Estimation of Drying Periods", "Description and Function of Industrial Dryers"],
    },
    "GEG311": {
        1: ["Introduction to Multivariable Calculus", "Limits and Continuity", "Introduction to Partial Derivatives"],
        2: ["Higher Order Derivatives", "Total Differential", "Applications of Partial Derivatives"],
        3: ["Taylor Series", "Constrained Optimization — Lagrange Multiplier", "Unconstrained Optimization — Euler-Lagrange"],
        4: ["Jacobian Matrix", "Hessian Matrix", "Introduction to Vectors"],
        5: ["Vector Algebra", "Vector Basis", "Einstein's Summation Convention"],
        6: ["Differentiation Under the Integral Sign", "Leibniz Theorem", "Line Integrals"],
        7: ["Green's Theorem", "Multiple Integrals", "Applications of Multiple Integrals"],
    },
    "GST307": {
        1: ["Introduction to Entrepreneurship", "Identifying Business Opportunities", "Business Planning Basics"],
        2: ["Corporate Governance Principles", "Stakeholder Management", "Ethics in Business"],
        3: ["Financing a Business", "Marketing and Sales Strategy", "Operations Management"],
        4: ["Corporate Social Responsibility", "Legal Framework for Businesses in Nigeria", "Entrepreneurship Case Studies"],
    },
    "MEG319": {
        1: ["Power Transmission by Screw Threads", "Power Transmission by Friction Clutches", "Power Transmission by Belt Drives"],
        2: ["Simple Gear Trains", "Epicyclic Gear Trains", "Vibration of Simple Mechanical Systems in Translation"],
        3: ["Vibration in Rotation and Torsion", "Damping Mechanisms in Mechanical Engineering Systems", "Review and Problem Solving"],
    },
    "PGG311": {
        1: ["Lab Safety and Equipment Overview", "Rock Sample Preparation and Analysis", "Porosity Measurement Techniques"],
        2: ["Permeability Measurement", "Fluid Saturation Determination", "Core Analysis Procedures"],
        3: ["Viscosity Measurement", "PVT Analysis Basics", "Lab Report Writing and Data Interpretation"],
    },
    "PGG313": {
        1: ["Introduction to Petroleum and Gas Engineering", "Petroleum Reservoir Fundamentals", "Reservoir Rock Properties"],
        2: ["Reservoir Fluid Properties", "Crude Oil Properties", "Equations of State"],
        3: ["Introduction to Steady and Unsteady Equations of State", "Undersaturated Reservoirs", "Saturated Reservoirs"],
        4: ["Reservoir Drive Mechanisms", "General Material Balance", "Introduction to Secondary Recovery Methods"],
        5: ["Waterflooding", "Gas Flooding", "Basic Concepts in Immiscible Fluid Displacement"],
        6: ["Fractional Flow Equations", "Buckley-Leverett Method", "Overview of Enhanced Oil Recovery"],
        7: ["EOR Screening Criteria", "Miscible Gas Injection Processes", "Chemical Flooding Processes"],
        8: ["Thermal EOR Processes", "Implementation of EOR Projects", "Introduction to Reservoir Phase Behavior"],
        9: ["Oil Field Economics", "Economic Evaluation of Petroleum Projects", "Review and Problem Solving"],
    },
    "PGG314": {
        1: ["Introduction to Gas Processing Equipment", "Separators — Types and Design", "Gas Scrubbers and Filters"],
        2: ["Heat Exchangers in Gas Processing", "Compressors — Types and Applications", "Pumps and Valves"],
        3: ["Absorption Columns", "Distillation Columns", "Dehydration Equipment"],
        4: ["Instrumentation and Control in Gas Plants", "Safety Systems in Gas Plants", "Equipment Sizing and Review"],
    },
    "PGG317": {
        1: ["Introduction to Drilling Engineering", "Drilling Personnel and Responsibilities", "The Drilling Proposal and Program"],
        2: ["Rotary Drilling Equipment", "The Drilling Process Step by Step", "Drilling Fluids — Types and Functions"],
        3: ["Drill Bits — Types and Selection", "Casing Design and Installation", "Cementing Operations"],
        4: ["Well Control — Kicks and Blowouts", "Directional Drilling Basics", "Drilling Economics and Cost Estimation"],
    },
    "PGG318": {
        1: ["Composition and Characteristics of Natural Gas", "Natural Gas as a Strategic Resource in Nigeria", "Overview of Nigeria's Gas Reserves and Production"],
        2: ["Significance of Natural Gas Processing", "Introduction to Gas Conditioning and Treatment", "Separation Processes — Water, Condensate and Solids Removal"],
        3: ["Acid Gas Removal — H₂S and CO₂", "Absorption and Adsorption Techniques", "Acid Gas Management in Nigerian Gas Fields"],
        4: ["Need for Gas Dehydration", "Glycol Dehydration Method", "Solid Desiccant Dehydration Techniques"],
        5: ["Selection and Optimization of Dehydration Techniques", "Design of Dehydration Facilities", "Composition and Uses of Natural Gas Liquids"],
        6: ["Extraction of NGLs — Cryogenic Process", "Lean Oil Absorption for NGL Recovery", "Fractionation Process and Equipment"],
        7: ["Design of Fractionation Units in Nigerian Facilities", "Fundamentals of LNG Processing", "LNG Production — Pretreatment and Liquefaction"],
        8: ["LNG Storage and Transportation", "Case Studies — NLNG Nigeria", "Importance of Gas Compression"],
        9: ["Types of Compressors in Gas Facilities", "Pipeline Design and Safety Standards", "Regulatory Standards and Environmental Considerations in Nigeria"],
        10: ["Environmental Impact of Natural Gas Processing", "Nigerian Policies and Regulatory Framework", "Economic Analysis of Gas Processing Projects"],
        11: ["Sustainable Gas Processing Practices", "Future of Nigeria's Gas Industry", "Review and Problem Solving"],
    },
    "CHG341": {
        1: ["Definition and Modes of Heat Transfer", "Mechanism of Conduction in Solids, Liquids and Gases", "Fourier's Law of Heat Conduction"],
        2: ["Application of Fourier's Law to Cylinders and Spheres", "Composite Walls and Numerical Solutions", "Principles of Free and Forced Convection"],
        3: ["Film Heat Transfer Coefficient", "Combined Conduction and Convection", "Heat Exchanger Design"],
        4: ["Boiling and Condensation — Phases and Heat Flux", "Film and Drop Condensation", "Mechanism of Radiation Heat Transfer"],
        5: ["Shape Factors in Radiation", "Heat Exchange Between Radiating Surfaces", "Navier-Stokes Equation and Problem Formulation"],
    },
    "CHG342": {
        1: ["Physical Properties Important to Separation Processes", "Stagewise Exchange and Equilibrium Stages", "Leaching and Extraction with Immiscible Solvents"],
        2: ["Binary Distillation — Fundamentals", "Binary Distillation — Design and Calculations", "Continuous Contact Columns — NTU and HTU"],
        3: ["Application to Hydrodynamics", "Limitations and Performance Data", "Review and Problem Solving"],
    },
    "CHG343": {
        1: ["Introduction to Thermodynamics", "Basic Concepts — Work, Heat, Energy and Equilibrium", "First Law of Thermodynamics"],
        2: ["State and Path Functions", "Enthalpy and the Phase Rule", "Constant Volume and Constant Pressure Processes"],
        3: ["Heat Capacity", "Steady State Flow Processes", "Ideal Gas Behavior"],
        4: ["PVT Behavior of Pure Substances", "Equations of State", "Thermodynamic Charts and Tables"],
        5: ["Steady Flow Devices — Boilers, Condensers, Nozzles", "Second Law of Thermodynamics and Entropy", "Power Cycles — Carnot and Rankine"],
        6: ["Refrigeration Cycle", "Internal Combustion and Diesel Engines", "Gas Turbines, Jet and Rocket Engines"],
    },
    "GEG322": {
        1: ["Introduction to Fourier Series", "Periodic Functions, Odd and Even Functions", "Half-Range Fourier Series and Cosine Series"],
        2: ["Parseval's Identity", "Differentiation and Integration of Fourier Series", "Boundary Value Problems"],
        3: ["Laplace Transformation and Applications", "Classification of PDEs — Elliptical, Parabolic and Hyperbolic", "Method of Separation of Variables"],
        4: ["Laplace Equation in Rectangular, Cylindrical and Spherical Coordinates", "Navier-Stokes Equation", "Maxwell Equations of Electromagnetism"],
    },
    "PGG321": {
        1: ["Fluid and Rock Properties Review", "Darcy's Law", "Classification of Reservoir Flow Systems"],
        2: ["Steady State Linear Flow of Incompressible Fluids", "Steady State Linear Flow of Compressible Fluids", "Linear Beds in Series and Parallel"],
        3: ["Poiseuille's Law of Capillary Flow", "Flow Through Fractures", "Steady State Radial Flow of Incompressible Fluids"],
        4: ["Steady State Radial Flow of Compressible Fluids", "Permeability Variations", "Unsteady Flow in Bounded Drainage Areas"],
        5: ["Average Pressure in Radial Flow Systems", "Re-adjustment Time", "Productivity Index and Zonal Damage"],
        6: ["Well Stimulation", "Deliverability, Well Spacing and Recovery", "Displacement of Oil and Gas"],
    },
    "PGG322": {
        1: ["Goals of Formation Evaluation", "Methods of Formation Evaluation — Mud Logging and Coring", "Formation Sampling and Testing"],
        2: ["Wireline Logging Operations", "Basic Logging Tools — Gamma Ray and Spontaneous Potential", "Density, Sonic and Neutron Logs"],
        3: ["Induction and Resistivity Logs", "Log Interpretation — Lithology and Porosity", "Log Interpretation — Permeability and Fluid Saturations"],
        4: ["Introduction to Geophysical Exploration", "Gravity and Magnetic Methods", "Electrical and Induced Polarization Methods"],
        5: ["Seismic Refraction and Reflection Methods", "Corrections to Field Anomalies", "Borehole Geophysical Well Logging"],
    },
    "PGG324": {
        1: ["Exploration and Production Licences", "Exploration, Development and Abandonment", "Drilling Personnel and Responsibilities"],
        2: ["The Drilling Proposal and Drilling Program", "Rotary Drilling Equipment", "The Drilling Process"],
        3: ["Offshore Drilling", "Drilling Costs in Field Development", "Drilling Cost Estimates and Economics"],
    },
    "PGG325": {
        1: ["Lab Safety and Equipment Familiarisation", "Experiment Design and Data Collection", "Lab Report Writing"],
        2: ["Core Analysis Experiments", "Fluid Properties Experiments", "Data Analysis and Interpretation"],
        3: ["Reservoir Simulation Lab Exercises", "Formation Evaluation Lab Exercises", "Final Lab Review"],
    },
    "PGG327": {
        1: ["Introduction to Industrial Training", "Health and Safety in the Workplace", "Professional Ethics and Conduct"],
        2: ["Writing Your SWEP Report", "Documenting Field Experience", "Presenting Technical Work"],
        3: ["Review of Field Experience", "Lessons from Industry", "Career Planning in Petroleum Engineering"],
    },
        "GEG411": {
        1: ["Technical Writing Fundamentals", "Report Writing and Documentation", "Oral Presentation Skills"],
        2: ["Scientific Paper Writing", "Engineering Proposals and Memos", "Visual Communication and Data Presentation"],
    },
    "MME412": {
        1: ["Introduction to Corrosion", "Electrochemical Principles", "Types of Corrosion"],
        2: ["Corrosion in Oil and Gas Systems", "Corrosion Monitoring Techniques", "Corrosion Inhibitors"],
        3: ["Cathodic and Anodic Protection", "Protective Coatings", "Material Selection for Corrosion Resistance"],
        4: ["Corrosion in Pipelines and Offshore Structures", "Case Studies in Nigerian Oil Fields", "Review and Problem Solving"],
    },
    "PGG431": {
        1: ["Reservoir Fluid Properties Review", "Material Balance Equation — Derivation", "Material Balance for Undersaturated Reservoirs"],
        2: ["Material Balance for Gas Reservoirs", "Material Balance for Solution Gas Drive", "Havlena-Odeh Method"],
        3: ["Decline Curve Analysis — Exponential", "Decline Curve Analysis — Hyperbolic and Harmonic", "Production Forecasting"],
        4: ["Pressure Transient Analysis Fundamentals", "Buildup and Drawdown Tests", "Reservoir Limit Testing"],
        5: ["Multiphase Flow in Reservoirs", "Relative Permeability Concepts", "Fractional Flow Theory"],
        6: ["Review of EOR Methods", "Reservoir Simulation Overview", "Field Development Planning"],
    },
    "PGG432": {
        1: ["Introduction to Production Engineering", "Inflow Performance Relationship — IPR", "Vogel and Standing Correlations"],
        2: ["Tubing Performance and Nodal Analysis", "Multiphase Flow in Wellbores", "Vertical Lift Performance"],
        3: ["Artificial Lift Methods — Overview", "Electric Submersible Pumps", "Gas Lift Design and Operation"],
        4: ["Surface Production Facilities", "Wellhead and Christmas Tree Equipment", "Separators and Treaters"],
        5: ["Production Chemicals and Flow Assurance", "Wax, Scale and Hydrate Management", "Pipeline Production Systems"],
        6: ["Well Completion Design", "Perforation and Stimulation", "Production Optimization"],
    },
    "PGG433": {
        1: ["Introduction to Gas Dynamics", "Thermodynamic Properties of Gases", "Compressible Flow Fundamentals"],
        2: ["Isentropic Flow Relations", "Normal Shock Waves", "Oblique Shock Waves"],
        3: ["Flow Through Nozzles and Diffusers", "Fanno Flow — Friction Effects", "Rayleigh Flow — Heat Transfer Effects"],
        4: ["Gas Flow in Pipelines", "Compressor Station Design", "Gas Transmission Network Analysis"],
        5: ["Pressure Drop Calculations in Gas Systems", "Transient Flow in Gas Pipelines", "Review and Problem Solving"],
    },
    "PGG434": {
        1: ["Introduction to Well Logging", "Borehole Environment and Invasion", "Spontaneous Potential Log"],
        2: ["Gamma Ray Log — Interpretation", "Resistivity Logs — Principles", "Resistivity Logs — Interpretation"],
        3: ["Porosity Logs — Density Log", "Porosity Logs — Neutron Log", "Sonic Log and Acoustic Properties"],
        4: ["Log Interpretation — Lithology Identification", "Water Saturation Determination — Archie's Equation", "Permeability Estimation from Logs"],
        5: ["Formation Evaluation Workflow", "Cross-Plot Techniques", "Case Studies from Nigerian Formations"],
    },
    "PGG435": {
        1: ["Lab Safety and Equipment Overview", "Core Flooding Experiments", "Special Core Analysis"],
        2: ["PVT Cell Experiments", "Separator Test Procedures", "Well Test Data Analysis"],
        3: ["Production Log Interpretation", "Reservoir Simulation Lab Exercises", "Final Lab Review and Report Writing"],
    },
    "PGG436": {
        1: ["Introduction to Well Testing", "Pressure Transient Theory", "Wellbore Storage and Skin Effects"],
        2: ["Pressure Buildup Analysis — Horner Method", "Drawdown Test Analysis", "Multi-Rate Testing"],
        3: ["Interference and Pulse Testing", "Gas Well Testing", "Naturally Fractured Reservoir Testing"],
        4: ["Modern Well Test Interpretation — Type Curves", "Derivative Analysis", "Case Studies and Field Examples"],
    },
    "PGG451": {
        1: ["Introduction to Reservoir Simulation", "Grid Systems and Discretization", "Single Phase Flow Simulation"],
        2: ["Multiphase Flow Simulation", "History Matching Fundamentals", "Sensitivity Analysis"],
        3: ["Introduction to Petrel and Eclipse", "Building a Simulation Model", "Production Forecasting with Simulators"],
        4: ["Introduction to PROSPER and GAP", "Nodal Analysis with Software", "Integrated Asset Modelling"],
        5: ["Data Input and Quality Control", "Simulation Results Interpretation", "Field Development Scenarios"],
    },
    "PGG453": {
        1: ["Introduction to Subsea Engineering", "Subsea Field Development Concepts", "Subsea Wellheads and Trees"],
        2: ["Subsea Manifolds and Templates", "Flexible and Rigid Risers", "Umbilicals and Control Systems"],
        3: ["Subsea Pipeline Design", "Flow Assurance in Subsea Systems", "Hydrate and Wax Management"],
        4: ["Subsea Processing Systems", "Installation Methods and Vessels", "Nigerian Deepwater Case Studies — Bonga, Agbami, Egina"],
        5: ["Integrity Management of Subsea Systems", "Inspection and Maintenance Strategies", "Review and Problem Solving"],
    },
}

LEVEL_CHOICES = [("100", "100 Level"), ("200", "200 Level"), ("300", "300 Level"), ("400", "400 Level"), ("500", "500 Level")]
SEMESTER_CHOICES = [("1", "First Semester"), ("2", "Second Semester")]
SCHOOL_CHOICES = [("unilag", "University of Lagos")]
DEPARTMENT_CHOICES = [("petroleum", "Petroleum & Gas Engineering")]
class CourseDefinition(models.Model):
    """Admin-defined course catalogue — compulsory or elective"""
    course_code = models.CharField(max_length=10, unique=True)
    course_title = models.CharField(max_length=100)
    level = models.CharField(max_length=3, choices=LEVEL_CHOICES)
    semester = models.CharField(max_length=1, choices=SEMESTER_CHOICES)
    school = models.CharField(max_length=50, choices=SCHOOL_CHOICES, default="unilag")
    department = models.CharField(max_length=50, choices=DEPARTMENT_CHOICES, default="petroleum")
    units = models.IntegerField(default=3)
    is_elective = models.BooleanField(default=False)

    class Meta:
        ordering = ["level", "semester", "is_elective", "course_code"]

    def __str__(self):
        tag = "Elective" if self.is_elective else "Compulsory"
        return f"{self.course_code} — {self.course_title} ({tag})"


class StudentProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    matric_number = models.CharField(max_length=20, blank=True)
    school = models.CharField(max_length=50, choices=SCHOOL_CHOICES, default="unilag")
    department = models.CharField(max_length=50, choices=DEPARTMENT_CHOICES, default="petroleum")
    level = models.CharField(max_length=3, choices=LEVEL_CHOICES)
    semester = models.CharField(max_length=1, choices=SEMESTER_CHOICES)
    is_staff_member = models.BooleanField(default=False)
    elective_courses = models.ManyToManyField(
        'CourseDefinition',
        blank=True,
        related_name="enrolled_students",
        limit_choices_to={"is_elective": True}
    )
    xp = models.IntegerField(default=0)
    streak = models.IntegerField(default=0)
    last_session_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} — {self.level}L"

class TimetableEntry(models.Model):
    DAYS = [("Mon", "Monday"), ("Tue", "Tuesday"), ("Wed", "Wednesday"), ("Thu", "Thursday"), ("Fri", "Friday")]

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="timetable")
    course_code = models.CharField(max_length=10)
    course_title = models.CharField(max_length=100)
    day = models.CharField(max_length=3, choices=DAYS)
    time = models.CharField(max_length=10, default="09:00")
    week_number = models.IntegerField(default=1)
    total_weeks = models.IntegerField(default=15)
    is_completed = models.BooleanField(default=False)
    is_missed = models.BooleanField(default=False)
    rescheduled_to = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["day", "time"]

    def __str__(self):
        return f"{self.student.user.username} — {self.course_code} ({self.day})"


class Session(models.Model):
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="sessions")
    course_code = models.CharField(max_length=10)
    course_title = models.CharField(max_length=100)
    week_number = models.IntegerField(default=1)
    topics = models.JSONField(default=list)
    current_topic_index = models.IntegerField(default=0)
    is_complete = models.BooleanField(default=False)
    xp_earned = models.IntegerField(default=0)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.student.user.username} — {self.course_code} Week {self.week_number}"


class TopicSession(models.Model):
    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="topic_sessions")
    topic_name = models.CharField(max_length=200)
    topic_index = models.IntegerField(default=0)
    intro_content = models.TextField(blank=True)
    lecture_content = models.TextField(blank=True)
    quiz_question = models.TextField(blank=True)
    quiz_options = models.JSONField(default=list)
    correct_answer_index = models.IntegerField(default=0)
    quiz_explanation = models.TextField(blank=True)
    student_answer_index = models.IntegerField(null=True, blank=True)
    passed_quiz = models.BooleanField(default=False)
    xp_earned = models.IntegerField(default=0)
    is_complete = models.BooleanField(default=False)

    class Meta:
        ordering = ["topic_index"]

    def __str__(self):
        return f"{self.session} — {self.topic_name}"
    
class ChatMessage(models.Model):
    ROLE_CHOICES = [
        ("user", "User"),
        ("ai", "AI"),
    ]

    topic_session = models.ForeignKey(
        TopicSession, on_delete=models.CASCADE, related_name="chatmessage_set"
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.role} — {self.content[:40]}"


class SlideDocument(models.Model):
    course_code = models.CharField(max_length=10)
    course_title = models.CharField(max_length=100)
    level = models.CharField(max_length=3, choices=LEVEL_CHOICES)
    file = models.FileField(upload_to="slides/", max_length=500)
    extracted_text = models.TextField(blank=True)
    extracted_topics = models.JSONField(default=list)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    parsed = models.BooleanField(default=False)

    class Meta:
        ordering = ["course_code"]
        unique_together = ["course_code", "level"]

    def __str__(self):
        return f"{self.course_code} — Slide Document"


class CourseOutline(models.Model):
    course_code = models.CharField(max_length=10)
    course_title = models.CharField(max_length=100)
    level = models.CharField(max_length=3, choices=LEVEL_CHOICES)
    file = models.FileField(upload_to="outlines/")
    extracted_text = models.TextField(blank=True)
    topics_json = models.JSONField(default=dict)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    parsed = models.BooleanField(default=False)

    class Meta:
        unique_together = ["course_code", "level"]

    def __str__(self):
        return f"{self.course_code} — Course Outline"


class PastQuestion(models.Model):
    course_code = models.CharField(max_length=10)
    course_title = models.CharField(max_length=100)
    level = models.CharField(max_length=3, choices=LEVEL_CHOICES)
    file = models.FileField(upload_to="past_questions/")
    extracted_text = models.TextField(blank=True)
    parsed_questions = models.JSONField(default=list)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    parsed = models.BooleanField(default=False)

    class Meta:
        ordering = ["course_code", "-uploaded_at"]

    def __str__(self):
        return f"{self.course_code} — Past Questions ({self.uploaded_at.strftime('%Y')})"


class Test(models.Model):
    STATUS_CHOICES = [
        ("pending", "Not Started"),
        ("in_progress", "In Progress"),
        ("complete", "Complete"),
    ]

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="tests")
    course_code = models.CharField(max_length=10)
    course_title = models.CharField(max_length=100)
    week_triggered = models.IntegerField(default=6)
    questions = models.JSONField(default=list)
    answers = models.JSONField(default=list)
    score = models.IntegerField(default=0)
    xp_earned = models.IntegerField(default=0)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default="pending")
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.student.user.username} — {self.course_code} Test (Week {self.week_triggered})"


class Exam(models.Model):
    STATUS_CHOICES = [
        ("pending", "Not Started"),
        ("in_progress", "In Progress"),
        ("complete", "Complete"),
    ]

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="exams")
    course_code = models.CharField(max_length=10)
    course_title = models.CharField(max_length=100)
    week_triggered = models.IntegerField(default=12)
    questions = models.JSONField(default=list)
    answers = models.JSONField(default=list)
    score = models.IntegerField(default=0)
    xp_earned = models.IntegerField(default=0)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default="pending")
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.student.user.username} — {self.course_code} Exam (Week {self.week_triggered})"


class Challenge(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending Acceptance"),
        ("active", "In Progress"),
        ("complete", "Complete"),
        ("declined", "Declined"),
        ("expired", "Expired"),
    ]

    # Changed on_delete to CASCADE to maintain relational database integrity if an active profile is dropped
    challenger = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="challenges_sent")
    opponent = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="challenges_received")
    course_code = models.CharField(max_length=10)
    course_title = models.CharField(max_length=100)
    questions = models.JSONField(default=list)
    challenger_answers = models.JSONField(default=list)
    opponent_answers = models.JSONField(default=list)
    challenger_score = models.IntegerField(default=0)
    opponent_score = models.IntegerField(default=0)
    winner = models.ForeignKey(
        StudentProfile, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="challenges_won"
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.challenger.user.username} vs {self.opponent.user.username} — {self.course_code}"